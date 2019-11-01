#
# Copyright (c) 2010, 2014 Oracle and/or its affiliates. All rights reserved.
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
copy_db_rpl test.
"""

import os
import time

import replicate

from mysql.utilities.exception import MUTLibError, UtilError
from mysql.utilities.common.replication import Master, Slave


_MASTER_DB_CMDS = [
    "DROP DATABASE IF EXISTS master_db1",
    "CREATE DATABASE master_db1",
    "CREATE TABLE master_db1.t1 (a int)",
    "INSERT INTO master_db1.t1 VALUES (1), (2), (3)",
]

_TEST_CASE_RESULTS = [
    # util result, db check results before, db check results after as follows:
    # BEFORE:
    #   SHOW DATABASES LIKE 'util_test'
    #   SELECT COUNT(*) FROM util_test.t1
    #   SHOW DATABASES LIKE 'master_db1'
    #   SELECT COUNT(*) FROM master_db1.t1
    # AFTER:
    #   SHOW DATABASES LIKE 'util_test'
    #   SELECT COUNT(*) FROM util_test.t1
    #   SHOW DATABASES LIKE 'master_db1'
    #   SELECT COUNT(*) FROM master_db1.t1
    #   <insert 2 rows into master_db1.t1>
    #   SELECT COUNT(*) FROM master_db1.t1
    [0, 'util_test', '7', None, False, 'util_test', '7', 'master_db1', '3',
     '5'],
    [0, None, False, None, False, 'util_test', '7', 'master_db1', '5', '7'],
    [0, None, False, None, False, 'util_test', '7', 'master_db1', '7', '9'],
    [0, None, False, None, False, 'util_test', '7', 'master_db1', '9', '11'],
]

_MAX_ATTEMPTS = 10   # Max tries to wait for slave before failing.
_SYNC_TIMEOUT = 30   # Number of seconds to wait for slaves to sync with master


class test(replicate.test):
    """test mysqldbcopy replication features
    This test executes the replication feature in mysqldbcopy to sync a slave
    and to test provisioning a slave from either a master or a slave. It uses
    the replicate test as a parent for testing methods.
    """

    # Test Cases:
    #    - copy extra db on master
    #    - provision a new slave from master
    #    - provision a new slave from existing slave

    server3 = None
    s3_serverid = None

    def check_prerequisites(self):
        # Need at least one server.
        self.server1 = None
        self.server2 = None
        return self.check_num_servers(1)

    def setup(self):
        self.res_fname = "result.txt"
        result = replicate.test.setup(self)

        # Note: server1 is master, server2, server3 are slaves.
        #       server3 is a new slave with nothing on it.

        self.server3 = self.servers.spawn_server(
            "new_slave", kill=True, mysqld='"--log-bin=mysql-bin"')

        self._drop_all()

        self.server1.exec_query("STOP SLAVE")
        self.server1.exec_query("RESET SLAVE")
        self.server2.exec_query("STOP SLAVE")
        self.server2.exec_query("RESET SLAVE")
        try:
            for cmd in _MASTER_DB_CMDS:
                self.server1.exec_query(cmd)
        except MUTLibError:
            raise

        data_file = os.path.normpath("./std_data/basic_data.sql")
        try:
            self.server1.read_and_exec_SQL(data_file, self.debug)
            self.server2.read_and_exec_SQL(data_file, self.debug)
        except MUTLibError as err:
            raise MUTLibError("Failed to read commands from file {0}: "
                              "{1}".format(data_file, err.errmsg))

        master_str = "--master={0}".format(
            self.build_connection_string(self.server1))
        slave_str = " --slave={0}".format(
            self.build_connection_string(self.server2))
        conn_str = master_str + slave_str

        cmd = "mysqlreplicate.py --rpl-user=rpl:rpl {0}".format(conn_str)
        try:
            self.exec_util(cmd, self.res_fname)
        except MUTLibError:
            raise

        # server1 is now a master server, lets treat it accordingly
        self.server1 = Master.fromServer(self.server1)
        try:
            self.server1.connect()
        except UtilError as err:
            raise MUTLibError("Cannot connect to spawned "
                              "server: {0}".format(err.errmsg))

        # server2 is now a slave, lets treat it accordingly
        self.server2 = Slave.fromServer(self.server2)
        try:
            self.server2.connect()
        except UtilError as err:
            raise MUTLibError("Cannot connect to spawned "
                              "server: {0}".format(err.errmsg))

        return result

    def wait_for_slave_connection(self, slave, attempts):
        """Wait for slave connection.

        Wait for slave to successfully connect to the master, waiting for
        events from him.

        slave[in]      Slave instance.
        Attempts[in]   Number of attempts.
        """
        i = 0
        while i < attempts:
            if self.debug:
                print(".")
            res = slave.exec_query("SHOW SLAVE STATUS")
            if res and res[0][0] == 'Waiting for master to send event':
                break
            # Wait 1 second before next iteration
            time.sleep(1)
            i += 1
            if i == attempts:
                raise MUTLibError("Slave did not sync with master.")
        return

    @staticmethod
    def _check_result(server, query):
        """Check result.

        Returns first query result, None if no result, False if error.

        server[in]    Server instance.
        query[in]     Query.
        """
        try:
            res = server.exec_query(query)
            if res:
                return res[0][0]
            else:
                return None
        except UtilError:
            return False

    # pylint: disable=W0221
    def run_test_case(self, actual_result, test_num, master, source,
                      destination, cmd_list, db_list, cmd_opts, comment,
                      expected_results, restart_replication=False,
                      skip_wait=False):

        results = [comment]

        # Drop all databases and reestablish replication
        if restart_replication:
            # Rollback here to avoid active transaction error for STOP SLAVE
            # with 5.5 servers (versions > 5.5.0).
            if self.servers.get_server(0).check_version_compat(5, 5, 0):
                destination.rollback()
            destination.exec_query("STOP SLAVE")
            destination.exec_query("RESET SLAVE")
            for db in db_list:
                self.drop_db(destination, db)
            master_str = "--master={0}".format(
                self.build_connection_string(master))
            slave_str = " --slave={0}".format(
                self.build_connection_string(destination))
            conn_str = master_str + slave_str

            cmd = "mysqlreplicate.py --rpl-user=rpl:rpl {0}".format(conn_str)
            try:
                self.exec_util(cmd, self.res_fname)
            except MUTLibError:
                raise

        # Convert object instance of master server to Master, if needed
        if not isinstance(master, Master):
            master = Master.fromServer(master)
            try:
                master.connect()
            except UtilError as err:
                raise MUTLibError("Cannot connect to spawned "
                                  "server: {0}".format(err.errmsg))

        # Convert object instance of destination server to Slave, if needed
        if not isinstance(destination, Slave):
            destination = Slave.fromServer(destination)
            try:
                destination.connect()
            except UtilError as err:
                raise MUTLibError("Cannot connect to spawned "
                                  "server: {0}".format(err.errmsg))

        # Check databases on slave and save results for 'BEFORE' check
        results.append(self._check_result(destination, "SHOW DATABASES "
                                                       "LIKE 'util_test'"))
        results.append(self._check_result(destination, "SELECT COUNT(*) "
                                                       "FROM util_test.t1"))
        results.append(self._check_result(destination, "SHOW DATABASES "
                                                       "LIKE 'master_db1'"))
        results.append(self._check_result(destination, "SELECT COUNT(*) "
                                                       "FROM master_db1.t1"))

        # Run the commands
        for cmd_str in cmd_list:
            try:
                res = self.exec_util(cmd_str + cmd_opts, self.res_fname)
                results.insert(1, res)  # save result at front of list
                if res != actual_result:
                    return False
            except MUTLibError:
                raise
        # Wait for slave to connect to master
        if not skip_wait:
            if self.debug:
                print("# Waiting for slave to connect to master",)
            try:
                self.wait_for_slave_connection(destination, _MAX_ATTEMPTS)
            except MUTLibError:
                raise
            if self.debug:
                print("done.")

        # Check databases on slave and save results for 'AFTER' check
        results.append(self._check_result(destination, "SHOW DATABASES "
                                                       "LIKE 'util_test'"))
        results.append(self._check_result(destination, "SELECT COUNT(*) "
                                                       "FROM util_test.t1"))
        results.append(self._check_result(destination, "SHOW DATABASES "
                                                       "LIKE 'master_db1'"))
        results.append(self._check_result(destination, "SELECT COUNT(*) "
                                                       "FROM master_db1.t1"))

        # Add something to master and check slave
        master.exec_query("INSERT INTO master_db1.t1 VALUES (10), (11)")
        # Wait for slave to catch up
        if not skip_wait:
            if self.debug:
                print("# Waiting for slave to sync")

            bin_info = master.get_binlog_info()
            if bin_info is None:  # server is no longer acting as a master
                raise MUTLibError("The server '{0}' is no longer a master"
                                  "server".format(master.role))

            # pylint: disable=W0633
            binlog_file, binlog_pos = bin_info

            # Wait for slave to catch up with master, using the binlog
            # Note: This test requires servers without GTIDs (prior to 5.6.5)
            synced = destination.wait_for_slave(binlog_file, binlog_pos,
                                                _SYNC_TIMEOUT)
            if not synced:
                raise MUTLibError("Slave did not catch up with master")
            if self.debug:
                print("done.")

        # ROLLBACK to close any active transaction leading to wrong values for
        # the next SELECT COUNT(*) with 5.5 servers (versions > 5.5.0).
        if self.servers.get_server(0).check_version_compat(5, 5, 0):
            destination.rollback()
        results.append(self._check_result(destination, "SELECT COUNT(*) "
                                                       "FROM master_db1.t1"))

        if self.debug:
            print(comment)
            print("Expected Results:", expected_results[test_num - 1])
            print("  Actual Results:", results[1:])

        self.results.append(results)

        return True

    def run(self):
        from_conn = "--source={0}".format(
            self.build_connection_string(self.server1))
        to_conn = "--destination={0}".format(
            self.build_connection_string(self.server2))
        db_list = ["master_db1"]

        cmd_str = ("mysqldbcopy.py {0} --rpl-user=rpl:rpl --skip-gtid {1} "
                   "{2} ".format(" ".join(db_list), from_conn, to_conn))

        # Copy master database
        test_num = 1
        comment = ("Test case {0} - Copy extra database from master "
                   "to slave".format(test_num))
        cmd_opts = "--rpl=master "
        res = self.run_test_case(0, test_num, self.server1, self.server1,
                                 self.server2, [cmd_str], db_list,
                                 cmd_opts, comment, _TEST_CASE_RESULTS, False)
        if not res:
            raise MUTLibError("{0}: failed".format(comment))
        test_num += 1

        to_conn = "--destination=" + self.build_connection_string(self.server3)
        db_list = ["util_test", "master_db1"]

        cmd_str = ("mysqldbcopy.py {0} --rpl-user=rpl:rpl --skip-gtid {1} "
                   "{2} ".format(" ".join(db_list), from_conn, to_conn))

        # Provision a new slave from master
        comment = ("Test case {0} - Provision a new slave from the "
                   "master".format(test_num))
        cmd_opts = "--rpl=master "
        res = self.run_test_case(0, test_num, self.server1, self.server1,
                                 self.server3, [cmd_str], db_list,
                                 cmd_opts, comment, _TEST_CASE_RESULTS, True)
        if not res:
            raise MUTLibError("{0}: failed".format(comment))
        test_num += 1

        from_conn = "--source={0}".format(
            self.build_connection_string(self.server2))
        to_conn = "--destination={0}".format(
            self.build_connection_string(self.server3))

        cmd_str = ("mysqldbcopy.py {0} --rpl-user=rpl:rpl --skip-gtid {1} "
                   "{2} ".format(" ".join(db_list), from_conn, to_conn))

        # Provision a new slave from existing slave
        comment = ("Test case {0} - Provision a new slave from existing "
                   "slave".format(test_num))
        cmd_opts = "--rpl=slave "
        res = self.run_test_case(0, test_num, self.server1, self.server2,
                                 self.server3, [cmd_str], db_list,
                                 cmd_opts, comment, _TEST_CASE_RESULTS, True)
        if not res:
            raise MUTLibError("{0}: failed".format(comment))

        test_num += 1
        # reset server3 removing it from the topology, in order to add it again
        # using the --rpl=slave option without specifying the rpl-user
        # BUG#18338321
        self.server3 = self.servers.spawn_server(
            "new_slave", kill=True, mysqld='"--log-bin=mysql-bin"')
        from_conn = "--source={0}".format(
            self.build_connection_string(self.server2))
        to_conn = "--destination={0}".format(
            self.build_connection_string(self.server3))

        cmd_str = ("mysqldbcopy.py {0} --skip-gtid {1} "
                   "{2} ".format(" ".join(db_list), from_conn, to_conn))

        # Provision a new slave from existing slave without --rpl-user
        comment = ("Test case {0} - Provision a new slave from existing "
                   "slave without --rpl-user".format(test_num))
        cmd_opts = "--rpl=slave "
        res = self.run_test_case(0, test_num, self.server1, self.server2,
                                 self.server3, [cmd_str], db_list,
                                 cmd_opts, comment, _TEST_CASE_RESULTS, False)
        if not res:
            raise MUTLibError("{0}: failed".format(comment))

        return True

    def get_result(self):
        # Here we check the result from execution of each test case.
        for i in range(0, len(_TEST_CASE_RESULTS)):
            if self.debug:
                print(self.results[i][0])
                print("  Actual results:", self.results[i][1:])
                print("Expected results:", _TEST_CASE_RESULTS[i])
            if self.results[i][1:] != _TEST_CASE_RESULTS[i]:
                msg = ("\n{0}\nExpected result = {1}\n  Actual result = "
                       "{2}\n".format(self.results[i][0], self.results[i][1:],
                                      _TEST_CASE_RESULTS[i]))
                return False, msg

        return True, ''

    def record(self):
        return True  # Not a comparative test

    def _drop_all(self):
        """Drop all databases created.
        """
        self.drop_db(self.server1, "util_test")
        self.drop_db(self.server1, "master_db1")
        self.drop_db(self.server2, "util_test")
        self.drop_db(self.server2, "master_db1")
        self.drop_db(self.server3, "util_test")
        self.drop_db(self.server3, "master_db1")
        return True

    def cleanup(self):
        if self.res_fname:
            os.unlink(self.res_fname)
        self._drop_all()
        # kill servers that are only used in this test
        kill_list = ['new_slave']
        return self.kill_server_list(kill_list)
