#
# Copyright 2011-2015 Jeff Bush
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

#
# Utility functions for unit tests. This is imported into test runner scripts
# in subdirectories under this one.
#

from __future__ import print_function
import subprocess
import os
import sys
import re
import traceback
import threading

COMPILER_DIR = '/usr/local/llvm-nyuzi/bin/'
PROJECT_TOP = os.path.normpath(
    os.path.dirname(os.path.abspath(__file__)) + '/../')
LIB_DIR = PROJECT_TOP + '/software/libs/'
BIN_DIR = PROJECT_TOP + '/bin/'
OBJ_DIR = 'obj/'
ELF_FILE = OBJ_DIR + 'test.elf'
HEX_FILE = OBJ_DIR + 'test.hex'


class TestException(Exception):

    def __init__(self, output):
        self.output = output

def build_program(source_files):
    """Compile/assemble one or more files.

    If there are .c files in the list, this will link in crt0.o, libc,
    and libos. It converts the binary to a hex file that can be loaded
    into memory.

    Args:
            source_files: List of files, which can be C/C++ or assembly
              files.

    Returns:
            Name of hex file created

    Raises:
            TestException if compilation failed, will contain compiler output
    """
    assert(isinstance(source_files, list))

    if not os.path.exists(OBJ_DIR):
        os.makedirs(OBJ_DIR)

    compiler_args = [COMPILER_DIR + 'clang',
                     '-o', ELF_FILE,
                     '-w',
                     '-O3']

    compiler_args += source_files

    needs_stdlib = False
    for file_name in source_files:
        if file_name.endswith('.c') or file_name.endswith('.cpp'):
            needs_stdlib = True
            break

    if needs_stdlib:
        compiler_args += ['-I' + LIB_DIR + 'libc/include',
                         '-I' + LIB_DIR + 'libos',
                         LIB_DIR + 'libc/crt0.o',
                         LIB_DIR + 'libc/libc.a',
                         LIB_DIR + 'libos/libos.a',
                         LIB_DIR + 'compiler-rt/compiler-rt.a']

    try:
        subprocess.check_output(compiler_args, stderr=subprocess.STDOUT)
        subprocess.check_output([COMPILER_DIR + 'elf2hex', '-o', HEX_FILE, ELF_FILE],
                                stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        raise TestException('Compilation failed:\n' + exc.output)

    return HEX_FILE

class TimedProcessRunner(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.finished = threading.Event()
        self.daemon = True  # Kill watchdog if we exit

    # Call process.communicate(), but fail if it takes too long
    def communicate(self, process, timeout):
        self.timeout = timeout
        self.process = process
        self.start()  # Start watchdog
        result = self.process.communicate()
        if self.finished.is_set():
            raise TestException('Test timed out')
        else:
            self.finished.set()  # Stop watchdog

        if self.process.poll():
            # Non-zero return code. Probably target program crash.
            raise TestException(
                'Process returned error: ' + result[0].decode())

        return result

    # Watchdog thread kills process if it runs too long
    def run(self):
        if not self.finished.wait(self.timeout):
            # Timed out
            self.finished.set()
            self.process.kill()


def _run_test_with_timeout(args, timeout):
    process = subprocess.Popen(args, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output, unused_err = TimedProcessRunner().communicate(process, timeout)
    return output.decode()


def run_program(
        environment='emulator',
        block_device=None,
        dump_file=None,
        dump_base=None,
        dump_length=None,
        timeout=60,
        flush_l2=False,
        trace=False):
    """Run test program.

    This uses the hex file produced by build_program.

    Args:
            environment: Which environment to execute in. Can be 'verilator'
               or 'emulator'.
            block_device: Relative path to a file that contains a filesystem image.
               If passed, contents will appear as a virtual SDMMC device.
            dump_file: Relative path to a file to write memory contents into after
               execution completes.
            dump_base: if dump_file is specified, base physical memory address to start
               writing mempry from.
            dump_length: number of bytes of memory to write to dump_file

    Returns:
            Output from program, anything written to virtual serial device

    Raises:
            TestException if emulated program crashes or the program cannot
              execute for some other reason.
    """

    if environment == 'emulator':
        args = [BIN_DIR + 'emulator']
        if block_device:
            args += ['-b', block_device]

        if dump_file:
            args += ['-d', dump_file + ',' +
                     hex(dump_base) + ',' + hex(dump_length)]

        args += [HEX_FILE]
        return _run_test_with_timeout(args, timeout)
    elif environment == 'verilator':
        args = [BIN_DIR + 'verilator_model']
        if block_device:
            args += ['+block=' + block_device]

        if dump_file:
            args += ['+memdumpfile=' + dump_file,
                     '+memdumpbase=' + hex(dump_base)[2:],
                     '+memdumplen=' + hex(dump_length)[2:]]

        if flush_l2:
            args += ['+autoflushl2=1']

        if trace:
            args += ['+trace']

        args += ['+bin=' + HEX_FILE]
        output = _run_test_with_timeout(args, timeout)
        if output.find('***HALTED***') == -1:
            raise TestException(output + '\nProgram did not halt normally')

        return output
    else:
        raise TestException('Unknown execution environment')


def assert_files_equal(file1, file2, error_msg='file mismatch'):
    """Read two files and throw a TestException if they are not the same

    Args:
            file1: relative path to first file
            file2: relative path to second file
            error_msg: If there is a file mismatch, prepend this to error output

    Returns:
            Nothing

    Raises:
            TestException if the files don't match. Exception test contains
            details about where the mismatch occurred.
    """

    BUFSIZE = 0x1000
    block_offset = 0
    with open(file1, 'rb') as fp1, open(file2, 'rb') as fp2:
        while True:
            block1 = fp1.read(BUFSIZE)
            block2 = fp2.read(BUFSIZE)
            if len(block1) < len(block2):
                raise TestException(error_msg + ': file1 shorter than file2')
            elif len(block1) > len(block2):
                raise TestException(error_msg + ': file1 longer than file2')

            if block1 != block2:
                for i in range(len(block1)):
                    if block1[i] != block2[i]:
                        # Show the difference
                        exception_text = error_msg + ':\n'
                        rounded_offset = i & ~15
                        exception_text += '%08x ' % (block_offset +
                                                     rounded_offset)
                        for x in range(16):
                            exception_text += '%02x' % ord(
                                block1[rounded_offset + x])

                        exception_text += '\n%08x ' % (
                            block_offset + rounded_offset)
                        for x in range(16):
                            exception_text += '%02x' % ord(
                                block2[rounded_offset + x])

                        exception_text += '\n         '
                        for x in range(16):
                            if block1[rounded_offset + x] \
                                    != block2[rounded_offset + x]:
                                exception_text += '^^'
                            else:
                                exception_text += '  '

                        raise TestException(exception_text)

            if not block1:
                return

            block_offset += BUFSIZE


registered_tests = []


def register_tests(func, names):
    """Add a list of tests to be run when execute_tests is called.

    This function can be called multiple times, it will append passed
    tests to the existing list.

    Args:
            func: A function that will be called for each of the elements
                    in the names list.
            names: List of tests to run.

    Returns:
            Nothing

    Raises:
            Nothing
     """

    global registered_tests
    registered_tests += [(func, name) for name in names]


def find_files(extensions):
    """Find files in the current directory that have certain extensions

    Args:
            extensions: list of extensions, each starting with a dot. For example
            ['.c', '.cpp']

    Returns:
            List of filenames

    Raises:
            Nothing
    """

    return [fname for fname in os.listdir('.') if fname.endswith(extensions)]

COLOR_RED = '[\x1b[31m'
COLOR_GREEN = '[\x1b[32m'
COLOR_NONE = '\x1b[0m]'


def execute_tests():
    """Run all tests that have been registered with the register_tests functions
    and report results. If this fails, it will call sys.exit with a non-zero status.

    Args:
            None

    Returns:
            None

    Raises:
            Nothing
    """

    global registered_tests

    if len(sys.argv) > 1:
        # Filter test list based on command line requests
        new_test_list = []
        for requested in sys.argv[1:]:
            for func, param in registered_tests:
                if param == requested:
                    new_test_list += [(func, param)]
                    break
            else:
                print('Unknown test ' + requested)
                sys.exit(1)

        registered_tests = new_test_list

    ALIGN = 40
    failing_tests = []
    for func, param in registered_tests:
        print(param + (' ' * (ALIGN - len(param))), end='')
        sys.stdout.flush()
        try:
            func(param)
            print(COLOR_GREEN + 'PASS' + COLOR_NONE)
        except KeyboardInterrupt:
            sys.exit(1)
        except TestException as exc:
            print(COLOR_RED + 'FAIL' + COLOR_NONE)
            failing_tests += [(param, exc.output)]
        except Exception as exc:
            print(COLOR_RED + 'FAIL' + COLOR_NONE)
            failing_tests += [(param, 'Test threw exception:\n' +
                               traceback.format_exc())]

    if failing_tests:
        print('Failing tests:')
        for name, output in failing_tests:
            print(name)
            print(output)

    print(str(len(failing_tests)) + '/' +
          str(len(registered_tests)) + ' tests failed')
    if failing_tests != []:
        sys.exit(1)


def check_result(source_file, program_output):
    """Check output of a program based on embedded comments in source code.

    For each pattern in a source file that begins with 'CHECK: ', search
    to see if the regular expression that follows it occurs in program_output.
    The strings must occur in order, but this ignores anything between them.
    If there is a pattern 'CHECKN: ', the test will fail if the string *does*
    occur in the output.

    Args:
            source_file: relative path to a source file that contains patterns

    Returns:
            Nothing

    Raises:
            TestException if a string is not found.
    """

    CHECK_PREFIX = 'CHECK: '
    CHECKN_PREFIX = 'CHECKN: '

    output_offset = 0
    lineNo = 1
    foundCheckLines = False
    with open(source_file, 'r') as f:
        for line in f:
            chkoffs = line.find(CHECK_PREFIX)
            if chkoffs != -1:
                foundCheckLines = True
                expected = line[chkoffs + len(CHECK_PREFIX):].strip()
                regexp = re.compile(expected)
                got = regexp.search(program_output, output_offset)
                if got:
                    output_offset = got.end()
                else:
                    error = 'FAIL: line ' + \
                        str(lineNo) + ' expected string ' + \
                        expected + ' was not found\n'
                    error += 'searching here:' + program_output[output_offset:]
                    raise TestException(error)
            else:
                chkoffs = line.find(CHECKN_PREFIX)
                if chkoffs != -1:
                    foundCheckLines = True
                    nexpected = line[chkoffs + len(CHECKN_PREFIX):].strip()
                    regexp = re.compile(nexpected)
                    got = regexp.search(program_output, output_offset)
                    if got:
                        error = 'FAIL: line ' + \
                            str(lineNo) + ' string ' + \
                            nexpected + ' should not be here:\n'
                        error += program_output
                        raise TestException(error)

            lineNo += 1

    if not foundCheckLines:
        raise TestException('FAIL: no lines with CHECK: were found')

    return True


def _run_generic_test(name):
    underscore = name.rfind('_')
    if underscore == -1:
        raise TestException(
            'Internal error: _run_generic_test did not have type')

    environment = name[underscore + 1:]
    basename = name[0:underscore]
    build_program([basename + '.c'])
    result = run_program(environment=environment)
    check_result(basename + '.c', result)


# XXX make this take a list of names
def register_generic_test(name):
    """Allows registering a test without having to create a test handler
    function. This will compile the passed program, then use
    check_result to validate it against comment strings embedded in the file.
    It runs it both in verilator and emulator configurations.

    Args:
            name: base name of source file (without extension)

    Returns:
            Nothing

    Raises:
            Nothing
    """
    register_tests(_run_generic_test, [name + '_verilator'])
    register_tests(_run_generic_test, [name + '_emulator'])


def _run_generic_assembly_test(name):
    underscore = name.rfind('_')
    if underscore == -1:
        raise TestException(
            'Internal error: _run_generic_test did not have type')

    environment = name[underscore + 1:]
    basename = name[0:underscore]

    build_program([basename + '.S'])
    result = run_program(environment=environment)
    if result.find('PASS') == -1 or result.find('FAIL') != -1:
        raise TestException('Test failed ' + result)

# XXX should this somehow be combined with register_generic_test?


def register_generic_assembly_tests(list):
    """Allows registering an assembly only test without having to
    create a test handler function. This will assemble the passed
    program, then look for PASS or FAIL strings.
    It runs it both in verilator and emulator configurations.

    Args:
            list: list of base names of source file (without extension)

    Returns:
            Nothing

    Raises:
            Nothing
    """
    for name in list:
        register_tests(_run_generic_assembly_test, [name + '_verilator'])
        register_tests(_run_generic_assembly_test, [name + '_emulator'])
