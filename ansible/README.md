# MariaDB replication via Ansible

## Setup (run once, locally where you have test-key.pem)

```bash
# Install ansible if not already present
sudo dnf install -y ansible-core   # Amazon Linux
# OR
sudo apt install -y ansible        # Ubuntu/Debian

# Make sure your key is in place and has correct perms
cp /path/to/test-key.pem ~/test-key.pem
chmod 400 ~/test-key.pem
```

## Test connectivity first

```bash
ansible db -m ping
```
All 3 DB hosts should return `pong`. If any fail, check security group (port 22) and the key path in inventory.ini.

## Run the playbook

```bash
ansible-playbook mariadb.yml
```

This will:
1. Install MariaDB on all 3 DB VMs
2. Configure the primary (server-id=1, binary logging)
3. Create the replication user + app database + app user
4. Grab the primary's binlog file/position automatically
5. Configure both replicas (unique server-id, read_only)
6. Point replicas at the primary and start replication
7. Print `SHOW SLAVE STATUS` output for both replicas at the end

## Verify replication worked

Look for these two lines in the final debug output for each replica:
```
Slave_IO_Running: Yes
Slave_SQL_Running: Yes
```
If both say `Yes`, replication is live. If either says `No`, check `Last_IO_Error` / `Last_SQL_Error` in the same output - that'll tell you exactly what broke.

## Quick manual test (optional, after playbook succeeds)

On primary (ssh in, `sudo mysql`):
```sql
USE crud_db;
CREATE TABLE test_repl (id INT);
INSERT INTO test_repl VALUES (1);
```

On a replica (ssh in, `sudo mysql`):
```sql
USE crud_db;
SELECT * FROM test_repl;
```
If you see the row, replication is confirmed working end to end.
