from __future__ import annotations

import os
import ssl
from pathlib import Path


def export_windows_roots(destination: Path) -> int:
    if os.name != "nt":
        raise RuntimeError("Windows certificate export is only available on Windows.")

    certificates: list[str] = []
    seen: set[bytes] = set()
    for certificate, encoding, _trust in ssl.enum_certificates("ROOT"):
        if encoding != "x509_asn" or certificate in seen:
            continue
        seen.add(certificate)
        certificates.append(ssl.DER_cert_to_PEM_cert(certificate))

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(certificates), encoding="ascii")
    return len(certificates)


def codex_transport_environment(cwd: Path) -> dict[str, str]:
    environment = dict(os.environ)
    if os.name != "nt" or environment.get("CODEX_CA_CERTIFICATE"):
        return environment

    bundle = cwd / ".agent-hub" / "certs" / "windows-roots.pem"
    if not bundle.is_file():
        export_windows_roots(bundle)
    environment["CODEX_CA_CERTIFICATE"] = str(bundle.resolve())
    return environment

