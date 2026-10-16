"""Local SQLite maintenance, no tokens or buyer data printed."""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['queue','retry','backup']);a=p.parse_args()
    path=ROOT/'data'/'bot.sqlite3'
    if not path.exists():
        print('Рабочая база ещё не создана. Сначала подключите и запустите бота.');return
    db=sqlite3.connect(path)
    try:
        if a.action=='queue':
            pending=db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0]
            print('Ожидают отправки:',pending)
            for code,count in db.execute("SELECT last_error,COUNT(*) FROM outbox WHERE sent=0 GROUP BY last_error"):
                print(code or 'ещё не отправлялось',count)
        elif a.action=='retry':
            with db:db.execute('UPDATE outbox SET next_try=0 WHERE sent=0')
            print('Ожидающие сообщения доступны для повторной отправки работающим ботом.')
        else:
            folder=ROOT/'data'/'backups';folder.mkdir(parents=True,exist_ok=True)
            dest=folder/('bot-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
            backup=sqlite3.connect(dest)
            try:db.backup(backup)
            finally:backup.close()
            print('Резервная копия создана:',dest.name)
    finally:db.close()

if __name__=='__main__':main()
