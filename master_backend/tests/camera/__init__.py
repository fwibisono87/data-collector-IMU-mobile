"""Camera-lane test package.

The ``__init__.py`` matters: without it pytest imports this directory's
``conftest.py`` under the plain module name ``conftest``, shadowing
``tests/conftest.py`` in ``sys.modules`` — and ``test_session_lifecycle.py``
and ``tests/contract/test_output_contract.py`` do ``from conftest import
FakeWs``, which would then resolve to the camera rig and fail collection.
As a package, the rig is imported as ``camera.conftest`` instead.
"""
