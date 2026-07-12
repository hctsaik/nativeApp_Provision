"""Production Registry / ObjectStore adapters (Slice 3).

These implement the same protocols as the stdlib SQLite / filesystem adapters
but depend on third-party drivers (``psycopg``, ``minio``). They deliberately
live OUTSIDE ``provision_builder`` so the builder runtime stays zero-third-party
(SPEC D2). Import is lazy: importing this package never imports the drivers, so
it is safe on machines without them — only constructing an adapter needs the
driver.

They are validated by the shared contract tests in ``tests/test_registry_contract``
and ``tests/test_object_store_contract`` when the matching endpoint env vars and
drivers are present (CI); on the locked-down build box those tests skip.
"""

from remote_adapters.minio_store import MinioObjectStore
from remote_adapters.postgres import PostgreSQLRegistry

__all__ = ["PostgreSQLRegistry", "MinioObjectStore"]
