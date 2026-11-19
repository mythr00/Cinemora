"""yt-dlp TLS trust regression tests (founder investigation 2026-11-19).

Root cause: yt-dlp verifies TLS against certifi's bundled Mozilla
roots by default and ignores the system trust store and
SSL_CERT_FILE. Behind a TLS-intercepting egress proxy whose CA lives
in the system store / environment bundle, every extraction failed
with CERTIFICATE_VERIFY_FAILED while curl/git/requests worked.

Fix contract: when the environment configures a CA bundle
(SSL_CERT_FILE or REQUESTS_CA_BUNDLE), every yt-dlp subprocess the
pipeline spawns must carry yt-dlp's supported
``--compat-options no-certifi`` so verification runs against the
system / environment trust chain. Verification is never disabled:
``--no-check-certificate`` must not appear anywhere in the module.
"""

import inspect
import os

import pytest

from services import media_pipeline


@pytest.fixture(autouse=True)
def _clean_ca_env(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    yield


def test_no_compat_flag_without_environment_bundle():
    command = media_pipeline._yt_dlp_base_command()
    assert command[:3] == [command[0], "-m", "yt_dlp"]
    assert "no-certifi" not in command


def test_ssl_cert_file_enables_system_trust(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/run/example/ca-bundle.pem")
    command = media_pipeline._yt_dlp_base_command()
    assert command[-2:] == ["--compat-options", "no-certifi"]


def test_requests_ca_bundle_enables_system_trust(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/run/example/ca-bundle.pem")
    command = media_pipeline._yt_dlp_base_command()
    assert command[-2:] == ["--compat-options", "no-certifi"]


def test_verification_is_never_disabled_in_module_source():
    source = inspect.getsource(media_pipeline)
    lowered = source.lower()
    # Quoted form: the flag as an actual argv token (prose mentions in
    # comments explaining the ban are unquoted and do not match).
    assert '"--no-check-certificate"' not in source
    assert "nocheckcertificate" not in lowered
    assert "cert_none" not in lowered


def test_all_invocation_sites_use_the_base_command():
    source = inspect.getsource(media_pipeline)
    # One definition plus the info-extraction and clip-download sites.
    assert source.count("_yt_dlp_base_command()") >= 3
    # The bare interpreter invocation survives only inside the helper.
    assert source.count('"-m", "yt_dlp"') == 1
