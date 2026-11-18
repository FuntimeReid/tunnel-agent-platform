"""Direct database account provisioning. Password input is hidden and never saved."""
import argparse
import getpass
from pathlib import Path

from store import Store, password_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=str(Path(__file__).with_name('private')))
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('add')
    create.add_argument('--employee-id', required=True)
    create.add_argument('--phone')
    create.add_argument('--name', required=True)
    create.add_argument('--role', choices=['superadmin', 'admin', 'user'], required=True)
    reset = commands.add_parser('set-password')
    reset.add_argument('--employee-id', required=True)
    args = parser.parse_args()
    password = getpass.getpass('输入新密码（至少10字符） ')
    if password != getpass.getpass('再次输入 ') or not 10 <= len(password) <= 256:
        raise SystemExit('两次密码不一致或长度不在10至256字符内。')
    store = Store(args.data)
    if args.command == 'add':
        store.add_account(args.employee_id, args.phone, args.name, args.role, password)
    else:
        with store.connect(write=True) as conn:
            row = conn.execute('SELECT id FROM account WHERE employee_id=?', (args.employee_id,)).fetchone()
            if not row:
                raise SystemExit('工号不存在。')
            conn.execute('UPDATE account SET password_hash=? WHERE id=?', (password_hash(password), row['id']))
            conn.execute('DELETE FROM session WHERE user_id=?', (row['id'],))
            store.audit(conn, None, 'account.password_reset', row['id'])
    print('账号数据库已更新。')


if __name__ == '__main__':
    main()
