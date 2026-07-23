from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        from django.db.backends.signals import connection_created

        def _sqlite_tune(sender, connection, **kwargs):
            """다중 워커 대비 SQLite 튜닝 — WAL(읽기·쓰기 동시성) + 잠금 대기."""
            if connection.vendor == "sqlite":
                with connection.cursor() as c:
                    c.execute("PRAGMA journal_mode=WAL;")
                    c.execute("PRAGMA busy_timeout=5000;")
                    c.execute("PRAGMA synchronous=NORMAL;")

        connection_created.connect(_sqlite_tune, dispatch_uid="sqlite-tune")
