#
# Copyright (c) 2010, 2016, Oracle and/or its affiliates. All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA
#

"""
This file contains the MySQL Utilities Test facility for running system
tests on the MySQL Utilities.
"""

import sys
import traceback

from mysql.utilities.common.tools import (check_python_version,
                                          check_connector_python)

# Check Python version compatibility
check_python_version(name='MySQL Utilities Test')

# Check for connector/python
if not check_connector_python():
    sys.exit(1)

import csv
import datetime
import os
import socket
import subprocess
import time
import mysql.connector
from mysql.utilities.common.server import Server, get_local_servers
from mysql.utilities.common.ip_parser import parse_connection
from mysql.utilities.common.options import add_verbosity
from mysql.utilities.common.options import get_ssl_dict, setup_common_options
from mysql.utilities.exception import MUTLibError, UtilError
from mutlib.mutlib import ServerList, get_port

# Constants
NAME = "MySQL Utilities Test - mut "
DESCRIPTION = "mut - run tests on the MySQL Utilities"
USAGE = "%prog --server=user:passwd@host:port test1 test2"

# Default settings
TEST_PATH = "./t"
UTIL_PATH = "../scripts"
SUITE_PATH = "./suite"
RESULT_PATH = "./r"
PRINT_WIDTH = 75
BOLD_ON = ''
BOLD_OFF = ''
if os.name == "posix":
    BOLD_ON = '\033[1m'
    BOLD_OFF = '\033[0m'
START_PORT = 3310
CONNECTION_CLOSE_WAIT_TIMEOUT = 4


# Shutdown any servers that are running
def _shutdown_running_servers(server_list, processes, basedir):
    """Shutdown any running servers.

    processes[in]       The list of processes to shutdown with the form:
                        (process_id, [datadir|port])
    basedir[in]         The starting path to search for mysqladmin tool

    Returns bool - True - servers shutdown attempted
                   False - no servers to shutdown
    """
    if len(processes) < 1:
        return False
    for process in processes:
        datadir = os.getcwd()
        connection = {
            "user": "root",
            "passwd": "root",
            "host": "localhost",
            "port": None,
        }
        if os.name == "posix":
            try:
                connection["port"] = get_port(process[0])
            except MUTLibError:
                connection["port"] = -1  # try to kill it forcefully later
        elif os.name == "nt":
            connection["port"] = process[1]

        if os.name == "posix":
            print("  Process id: {0:>6}, Data path: {1}".format(process[0],
                                                                process[1]))
        elif os.name == "nt":
            print("  Process id: {0:>6}, Port: {1}".format(process[0],
                                                           process[1]))

        # 1) connect to the server.
        server_options = {
            'conn_info': connection,
        }
        svr = Server(server_options)
        ok_to_shutdown = True
        try:
            svr.connect()
        except UtilError:  # if we cannot connect, don't try to shut it down.
            ok_to_shutdown = False
            print("    WARNING: shutdown failed - cannot connect.")
        if not ok_to_shutdown:
            if os.name == "posix":
                # Attempt kill
                ret_code = subprocess.call(["kill", "-9",
                                            " {0}".format(process[0])])
                # wait until connection is closed
                for _ in range(CONNECTION_CLOSE_WAIT_TIMEOUT):
                    try:
                        sock = socket.create_connection((svr.host, svr.port))
                        sock.close()
                        time.sleep(1)
                    except socket.error:
                        # could not connect, connection is closed
                        break
                else:
                    print("    WARNING: timeout waiting for connection to "
                          "process {0} to close".format(process[0]))
                if ret_code:
                    print("    WARNING: shutdown failed: Killing process {0}"
                          "returned error code {1}".format(process[0],
                                                           ret_code))
            else:
                ret_code = subprocess.call("taskkill /F /T /PID {0}".format(
                    process[0]))
                if ret_code not in (0, 128):
                    print("    WARNING: shutdown failed: Killing process {0} "
                          "returned error code {1}".format(process[0],
                                                           ret_code))
        # 2) if nt, verify datadirectory
        if os.name == "nt" and ok_to_shutdown:
            res = svr.show_server_variable("datadir")
            server_datadir = res[0][1]
            ok_to_shutdown = (server_datadir.find(datadir) >= 0)

        # 3) call shutdown method from mutlib Server_list class
        if ok_to_shutdown and svr:
            try:
                server_list.stop_server(svr)
            except MUTLibError:
                _, e, _ = sys.exc_info()
                print("    WARNING: shutdown failed: " + e.errmsg)
    return True


# Utility function
def _print_elapsed_time(start_test):
    """Print the elapsed time to stdout (screen)

    start_test[in]      The starting time of the test
    """
    stop_test = time.time()
    display_time = int((stop_test - start_test) * 100)
    if display_time == 0:
        display_time = 1
    sys.stdout.write(" {0:>6}\n".format(display_time))


# Utility function
def _report_error(message, test_name, mode, start_test, error=True):
    """Print an error message to stdout (screen)

    message[in]         Error message to print
    test_name[in]       The name of the current test
    mode[in]            Event mode (PASS, FAIL, WARN, etc)
    start_test[in]      The starting time of the test
    error[in]           If True, print 'ERROR' else print 'WARNING'
    """
    linelen = opt.width - (len(test_name) + 13)
    sys.stdout.write(' ' * linelen)
    sys.stdout.write("[{0}{1}{2}]".format(BOLD_ON, mode, BOLD_OFF))
    _print_elapsed_time(start_test)
    if error:
        fishy = "ERROR"
    else:
        fishy = "WARNING"
    print("\n{0}{1}{2}: {3}\n".format(BOLD_ON, fishy, BOLD_OFF, message))
    if mode == "FAIL":
        failed_tests.append(test)
    else:
        skipped_tests.append(test)


def _exec_and_report(procedure, default_message, test_name, action,
                     start_test_time, exception_procedure=None,
                     error=True):
    """Helper method to manage exception handling.
    """
    extra_message = None
    try:
        res = procedure()
        if res:
            return True
    except MUTLibError:
        _, e, _ = sys.exc_info()
        extra_message = e.errmsg
    _report_error(default_message, test_name, action, start_test_time,
                  error)
    # print the error if raised from the test.
    if extra_message is not None:
        print("%s\n" % extra_message)
    # if there is an exit strategy, execute it
    if exception_procedure is not None:
        exception_procedure()
    return False


def _read_disabled_tests():
    """Helper method to read CSV file.
    """
    disabled_tests = []
    with open("disabled") as f:
        csv_reader = csv.reader(f)
        for row in csv_reader:
            if row[0][0] != '#':
                disabled_tests.append(row)
    return disabled_tests


def find_tests(path):
    """Find test files.

    path[in]    Path to find tests.
    """
    test_files = []
    suites_found = []
    # Build the list of tests from suites to execute
    for root, _, files in os.walk(path):
        for f in files:
            # Check --suites list. Skip if we have suites and dir is in list
            start = len(path) + 1
            if root[-2:] in ['/t', '\\t']:
                end = len(root) - 2
            else:
                end = len(root)
            directory = root[start:end]
            if opt.suites:
                if directory == "":
                    if "main" not in opt.suites:
                        continue
                elif directory not in opt.suites:
                    continue
            if directory == "":
                directory = "main"

            if not directory == "main" and not directory in suites_found:
                suites_found.append(directory)

            # Get file and extension
            fname, ext = os.path.splitext(f)

            # Skip template tests
            if fname.endswith('_template'):
                continue

            # Check for suite.test as well as simply test
            if (args and fname not in args and
                    "{0}.{1}".format(directory, fname) not in args):
                continue

            # See if test is to be skipped
            if opt.skip_test:
                if (fname in opt.skip_test or
                        "{0}.{1}".format(directory, fname) in opt.skip_test):
                    continue

            # See if suite is to be skipped
            if opt.skip_suites:
                if directory in opt.skip_suites:
                    continue

            # Include only tests that are .py files and ignore mut
            # library files
            if ext == ".py" and fname != "__init__" and fname != "mutlib":
                test_ref = (directory, root, fname)

                # Do selective tests based on matches for --do-test=
                # Don't execute performance tests unless specifically
                # told to do so.
                if((opt.suites is not None and "performance" not in opt.suites
                        and directory == "performance")
                    or (directory == "performance" and
                        opt.suites is None)):
                    pass
                else:
                    if opt.wildcard:
                        for wild in opt.wildcard:
                            if wild == fname[0:len(wild)]:
                                test_files.append(test_ref)
                                break
                    elif opt.like:
                        for like in opt.like:
                            if like in fname:
                                test_files.append(test_ref)
                                break
                    elif opt.skip_tests:
                        for skip in opt.skip_tests:
                            if skip != fname[0:len(skip)]:
                                test_files.append(test_ref)
                                break
                    # Add test if no wildcard and suite (dir) is included
                    else:
                        test_files.append(test_ref)

    for suite in suites_found:
        sys.path.append(os.path.join(SUITE_PATH, suite, "t"))

    return test_files

if __name__ == "__main__":
    # Begin 'main' code
    parser = setup_common_options(os.path.basename(sys.argv[0]), DESCRIPTION,
                                  USAGE, False, False, add_ssl=True)

    # Add server option
    parser.add_option("--server", action="append", dest="servers",
                      help="connection information for a server to be used "
                      "in the tests in the form: user:passwd@host:port "
                      "- list option multiple times for multiple servers to "
                      "use")

    # Add test wildcard option
    parser.add_option("--do-tests", action="append", dest="wildcard",
                      type="string", help="execute all tests that begin "
                      "with this string. List option multiple times "
                      "to add multiple wildcards.")

    # Add test like option
    parser.add_option("--run-tests-like", action="append", dest="like",
                      type="string", help="execute all tests that contains "
                      "this string. List option multiple times "
                      "to add multiple wildcards.")

    # Add suite list option
    parser.add_option("--suite", action="append", dest="suites",
                      type="string", help="test suite to execute - list "
                      "option multiple times for multiple suites")

    # Add skip-test list option
    parser.add_option("--skip-test", action="append", dest="skip_test",
                      type="string", help="exclude a specific test - list "
                      "option multiple times for multiple tests")

    # Add skip-test list option
    parser.add_option("--skip-tests", action="append", dest="skip_tests",
                      type="string", help="exclude tests that begin with "
                      "this string. List option multiple times to add "
                      "multiple skips.")

    # Add start-test list option
    parser.add_option("--start-test", action="store", dest="start_test",
                      type="string", help="start executing tests that begin "
                      "with this string", default=None)

    # Add start-test list option
    parser.add_option("--stop-test", action="store", dest="stop_test",
                      type="string", help="stop executing tests at first "
                      "occurrence of test that begins with this string",
                      default=None)

    # Add skip-long tests option
    parser.add_option("--skip-long", action="store_true", dest="skip_long",
                      default=False, help="exclude tests that require "
                      "greater resources or take a long time to run")

    # Add skip-suite list option
    parser.add_option("--skip-suite", action="append", dest="skip_suites",
                      type="string", help="exclude suite - list "
                      "option multiple times for multiple suites")

    # Add test directory option
    parser.add_option("--testdir", action="store", dest="testdir",
                      type="string", help="path to test directory",
                      default=TEST_PATH)

    # Add starting port
    parser.add_option("--start-port", action="store", dest="start_port",
                      type="int", help="starting port for spawned servers",
                      default=START_PORT)

    # Add record option
    parser.add_option("--record", action="store_true", dest="record",
                      help="record output of specified test if successful")

    # Add sorted option
    parser.add_option("--sort", action="store", dest="sort",
                      default="asc", help="execute tests sorted by suite.name "
                      "either ascending (asc) or descending (desc).",
                      type="choice", choices=['asc', 'desc'])

    # Add utility directory option
    parser.add_option("--utildir", action="store", dest="utildir",
                      type="string", help="location of utilities",
                      default=UTIL_PATH)

    # Add display width option
    parser.add_option("--width", action="store", dest="width",
                      type="int", help="display width",
                      default=PRINT_WIDTH)

    # Force mode
    parser.add_option("-f", "--force", action="store_true", dest="force",
                      help="do not abort when a test fails")

    # Add verbosity mode
    add_verbosity(parser, False)

    # Skip cleanup mode
    parser.add_option("--skip-cleanup", action="store_true",
                      dest="skip_cleanup",
                      help="Do not shutdown spawned servers - debug only "
                           "mode.")

    # Now we process the rest of the arguments.
    opt, args = parser.parse_args()

    # Check for debug
    debug_mode = (opt.verbosity >= 2)
    verbose_mode = (opt.verbosity >= 1)

    # Cannot use --do-test= with listing tests.
    if opt.wildcard and len(args) > 0:
        parser.error("Cannot mix --do-test= and a list of tests.")

    # Cannot use --debug with listing tests.
    if debug_mode and len(args) > 1:
        parser.error("Cannot mix --debug and a list of tests.")

    # Must use --record with a specific test
    if opt.record and len(args) != 1:
        parser.error("Must specify a single test when using record.")

    # Expand the user's home component if used for Unix and Windows
    opt.testdir = os.path.expanduser(opt.testdir)
    opt.utildir = os.path.expanduser(opt.utildir)

    # If testdir is specified, modify variables accordingly
    if opt.testdir is not TEST_PATH:
        RESULT_PATH = os.path.join(opt.testdir, 'r')
        SUITE_PATH = os.path.join(opt.testdir, 'suite')
        TEST_PATH = os.path.join(opt.testdir, 't')

    # Append default paths if options not specified
    sys.path.append(TEST_PATH)
    sys.path.append(opt.utildir)

    # Process the server connections

    # print(preamble)
    print("\nMySQL Utilities Testing - MUT\n")
    print("Parameters used: ")
    print("  Display Width       = {0}".format(opt.width))
    print("  Sort                = {0}".format(opt.sort))
    print("  Force               = {0}".format(opt.force is not None))
    print("  Test directory      = '{0}'".format(opt.testdir))
    print("  Utilities directory = '{0}'".format(opt.utildir))
    print("  Starting port       = {0}".format(opt.start_port))
    print("  Connector/Python    = {0}".format(mysql.connector.__version__))

    # Check for suite list
    if opt.suites:
        sys.stdout.write("  Include only suites = ")
        for suite in opt.suites:
            sys.stdout.write("{0} ".format(suite))
        print()

    # Check to see if we're skipping suites
    if opt.skip_suites:
        sys.stdout.write("  Exclude suites      = ")
        for suite in opt.skip_suites:
            sys.stdout.write("{0} ".format(suite))
        print()

    # Is there a --do-test?
    if opt.wildcard:
        for wild in opt.wildcard:
            print("  Test wildcard       = '{0}%%'".format(wild))

    # Is there a --do-test?
    if opt.like:
        for wild in opt.like:
            print("  Test wildcard       = '%{0}%'".format(wild))

    # Check to see if we're skipping tests
    if opt.skip_test:
        sys.stdout.write("  Skipping tests      = ")
        for test in opt.skip_test:
            sys.stdout.write("{0} ".format(test))
        print()

    if opt.skip_tests:
        for skip in opt.skip_tests:
            print("  Skip wildcard       = '{0}%%'".format(skip))

    if opt.start_test:
        print("  Start test sequence = '{0}%%'".format(opt.start_test))
        start_sequence = True
    else:
        start_sequence = False

    if opt.stop_test:
        print("  Stop test           = '{0}%%'".format(opt.stop_test))

    server_list = ServerList([], opt.start_port, opt.utildir, opt.verbosity)
    basedir = None

    # Print status of connections
    print("\nServers:")
    if not opt.servers:
        print("  No servers specified.")
    else:
        i = 0
        for server in opt.servers:
            # add ssl options values.
            conn_options = {}
            conn_options.update(get_ssl_dict(opt))
            try:
                conn_val = parse_connection(server, options=conn_options)
            except UtilError as err:
                parser.error(err.errmsg)
            except:
                parser.error("Problem parsing server connection "
                             "'{0}'".format(server))

            i += 1
            # Fail if port is None
            if conn_val["port"] is None:
                parser.error("You must specify a port in the server "
                             "string: \n       {0}".format(server))

            sys.stdout.write("  Connecting to {0} as user {1} on port "
                             "{2}: ".format(conn_val["host"], conn_val["user"],
                                            conn_val["port"]))

            sys.stdout.flush()

            conn_val["port"] = int(conn_val["port"])

            # Remove unix socket from connection dictionary to avoid issue with
            # tests using the base server. Unix sockets are no longer used by
            # MUT.
            if "unix_socket" in conn_val:
                conn_val.pop("unix_socket")

            server_options = {
                'conn_info': conn_val,
                'role': "server{0}".format(i),
            }
            conn = Server(server_options)
            try:
                conn.connect()
                server_list.add_new_server(conn)
                print("CONNECTED")
                res = conn.show_server_variable("basedir")
                basedir = res[0][1]
                res = conn.exec_query("SELECT @@version")
                print("      MySQL Version: {0}".format(res[0][0]))
            # Here we capture any exception and print the error message.
            # Since all util errors (exceptions) derive from Exception, this is
            # safe.
            except:
                _, err, _ = sys.exc_info()
                print("{0}FAILED{1}".format(BOLD_ON, BOLD_OFF))
                if conn.connect_error is not None:
                    print(conn.connect_error)
                print("ERROR: {0!s}".format(err))
        if server_list.num_servers() == 0:
            print("ERROR: Failed to connect to any servers listed.")
            sys.exit(1)

    # Check for running servers
    processes = []
    if server_list.num_servers():
        processes = get_local_servers(False, opt.start_port, 3333, os.getcwd())

    # Kill any servers running from the test directory
    if len(processes) > 0:
        print("\nWARNING: There are existing servers running that may have "
              "been\nspawned by an earlier execution. Attempting shutdown.\n")
        _shutdown_running_servers(server_list, processes, basedir)

    test_files = []
    failed_tests = []
    skipped_tests = []

    test_files.extend(find_tests(SUITE_PATH))
    test_files.extend(find_tests(TEST_PATH))

    # If no tests, there's nothing to do!
    if len(test_files) == 0:
        print("No tests match criteria specified.")

    # Sort test cases
    test_files.sort(reverse=(opt.sort == 'desc'))

    # Check for validity of --start-test
    if start_sequence:
        found = False
        for test_tuple in test_files:
            if opt.start_test == test_tuple[2][0:len(opt.start_test)]:
                found = True
        if not found:
            print("\nWARNING: --start-test={0}%% was not found. Running full "
                  "suite(s)".format(opt.start_test))
            start_sequence = False

    # Check for validity of --stop-test
    if opt.stop_test:
        found = False
        for test_tuple in test_files:
            if opt.stop_test == test_tuple[2][0:len(opt.stop_test)]:
                found = True
        if not found:
            print("\nWARNING: --stop-test={0}%% was not found. Running full "
                  "suite(s)".format(opt.stop_test))
            opt.stop_test = None

    # Get list of disabled tests
    disable_list = _read_disabled_tests()

    have_disabled = len(disable_list)

    # print(header)
    print("\n{0}".format("-" * opt.width))
    print("".join(["TEST NAME", ' ' * (opt.width - 22), "STATUS   TIME"]))
    print("=" * opt.width)

    # Protect against interactive consoles that fail: save terminal settings.
    if os.name == "posix":
        import termios
        restore_terminal = True
        try:  # Guard against non supported tty devices
            old_terminal_settings = termios.tcgetattr(sys.stdin)
        except termios.error as err:
            restore_terminal = False
    exit_status = 0
    try:
        # Run the tests selected
        num_tests_run = 0
        stop_testing = False
        for test_tuple in test_files:

            # Skip tests for start-test sequence
            if start_sequence:
                if opt.start_test == test_tuple[2][0:len(opt.start_test)]:
                    start_sequence = False
                else:
                    continue

            # Skip tests for stop-test sequence
            if opt.stop_test and not stop_testing:
                stop_testing = (
                    opt.stop_test == test_tuple[2][0:len(opt.stop_test)])
            if stop_testing:
                continue

            # Get test parts - directory not used
            test = test_tuple[2]

            # Add path to suite
            test_name = "{0}.{1}".format(test_tuple[0], test)

            # record start time
            start_test = time.time()

            # Get result file location
            if test_tuple[0] == "main":
                result_loc = RESULT_PATH
            else:
                result_loc = os.path.join(SUITE_PATH, test_tuple[0], "r")

            # Create a reference to the test class
            test_class = __import__(test)
            test_case = test_class.test(server_list, result_loc,
                                        opt.utildir, verbose_mode, debug_mode)

            last_test = test_case
            # Print name of the test
            sys.stdout.write(test_name)
            sys.stdout.flush()

            # Skip disabled tests
            if have_disabled > 0:
                skipped = False
                for disabled_test in disable_list:
                    if test_name == disabled_test[0]:
                        _report_error("Test marked as disabled.", test_name,
                                      "SKIP", start_test, False)
                        skipped = True
                if skipped:
                    continue

            # Check to see if we need to skip long running tests
            if opt.skip_long and test_case.is_long():
                _report_error("Test marked as long running test.", test_name,
                              "SKIP", start_test, False)
                continue

            # Check prerequisites for number of servers. Skip test is there
            # are not enough servers to connect.
            if not _exec_and_report(test_case.check_prerequisites,
                                    "Cannot establish resources needed to run "
                                    "test.", test_name, "SKIP", start_test,
                                    None, False):
                continue

            # Set the preconditions for the test
            if not _exec_and_report(test_case.setup,
                                    "Cannot establish setup conditions to run "
                                    "test.", test_name, "SKIP", start_test,
                                    test_case.cleanup, False):
                continue

            # Run the test
            run_ok = True
            results = None
            run_msg = None
            try:
                run_ok = test_case.run()
                #
                # TODO: Replace this with the new result class from WL#7856
                #       Tests can turn off this global check with
                #       self.mask_global = False
                #
                mask_global = test_case.mask_global
                result_file = test_case.res_fname
                if mask_global:
                    test_case.remove_result("WARNING: Using a password on "
                                            "the comma")
                    test_case.remove_result("# WARNING: Using a password on "
                                            "the com")
                    test_case.remove_result("# WARNING: The use of InnoDB is")
            except MUTLibError:
                _, e, _ = sys.exc_info()
                if debug_mode:
                    # Using debug should result in a skipped result check.
                    pass
                else:
                    run_msg = e.errmsg
                    run_ok = False

            if run_ok:
                # Calculate number of spaces based on test name
                linelen = opt.width - (len(test_name) + 13)
                sys.stdout.write(' ' * linelen)

                # Record results of test
                if opt.record:
                    sys.stdout.write("RECORD")
                    res = test_case.record()
                    # Write time here since we're done
                    stop_test = time.time()
                    _print_elapsed_time(start_test)
                    if not res:
                        sys.stdout.write("  {0}WARNING{1}: Test record "
                                         "failed.".format(BOLD_ON, BOLD_OFF))

                elif debug_mode:
                    print("\nEnd debug results.\n")

                # Display status of test
                else:
                    run_ok = True
                    msg = None
                    try:
                        results = test_case.get_result()
                        if results[0]:
                            sys.stdout.write("[pass]")
                            num_tests_run += 1
                    except MUTLibError:
                        _, e, _ = sys.exc_info()
                        results = (False, ("Test results cannot be "
                                           "established.\n", e.errmsg + "\n"))
                        msg = e.errmsg

                    if not results[0]:
                        sys.stdout.write("[{0}FAIL{1}]\n".format(BOLD_ON,
                                                                 BOLD_OFF))
                        run_ok = False
                        failed_tests.append(test)

            else:
                _report_error("Test execution failed.", test_name, "FAIL",
                              start_test)
                print("{0}\n".format(run_msg))
                run_ok = False

            # Cleanup the database settings if needed
            test_cleanup_ok = True
            cleanup_msg = None
            try:
                test_cleanup_ok = test_case.cleanup()
            except MUTLibError:
                _, e, _ = sys.exc_info()
                cleanup_msg = e.errmsg
                test_cleanup_ok = False

            # Display the time if not recording
            if not opt.record and run_ok and not debug_mode:
                _print_elapsed_time(start_test)

            # Display warning about cleanup
            if not test_cleanup_ok:
                print("\n{0}WARNING{1}: Test cleanup failed.".format(BOLD_ON,
                                                                     BOLD_OFF))
                if cleanup_msg is not None:
                    print("{0}\n".format(cleanup_msg))

            if results is not None and results[1]:
                sys.stdout.write("\n{0}ERROR:{1} ".format(BOLD_ON, BOLD_OFF))
                for str_ in results[1]:
                    sys.stdout.write(str_)
                sys.stdout.write("\n")

            if verbose_mode:
                print(test_case.__doc__)

            # Check force option
            if not run_ok and not opt.force:
                break

        # print(postamble)
        print("-" * opt.width)
        print(datetime.datetime.now().strftime("Testing completed: "
                                               "%A %d %B %Y %H:%M:%S\n"))
        num_fail = len(failed_tests)
        num_skip = len(skipped_tests)
        num_tests = len(test_files)
        if num_fail == 0 and num_skip == 0:
            if num_tests > 0:
                print("All {0} tests passed.".format(num_tests))
        else:
            print("{0} of {1} tests completed.\n".format(num_tests_run,
                                                         num_tests))
            if num_skip:
                print("The following tests were skipped:\n")
                print(" ".join(skipped_tests))
                print("\n")
            if num_fail:
                print("The following tests failed:\n")
                print(" ".join(failed_tests))
                print("\n")
    # Catch and print exception stack trace
    except:
        print(traceback.format_exc())
        exit_status=1
    # kill spawned servers and restore terminal settings even if something
    # goes wrong halfway.
    finally:
        # Shutdown connections and spawned servers
        if not opt.skip_cleanup:
            if server_list.num_spawned_servers():
                print("\nShutting down spawned servers ")
                server_list.shutdown_spawned_servers()

            if server_list.cleanup_list > 0:
                sys.stdout.write("\nDeleting temporary files...")
                server_list.remove_files()
                print("success.")

        del server_list

        sys.stdout.write("\n")

        # Protect against interactive consoles that fail: restore terminal
        # settings.
        if os.name == "posix" and restore_terminal:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN,
                              old_terminal_settings)

        sys.exit(exit_status)
