from __future__ import annotations

import os
import ssl
from pathlib import Path

# 固定证书缓存根：不随任务 cwd 变化（P0-03）。
# 优先使用环境变量 AGENT_HUB_CERTS_ROOT，否则落在用户级固定目录。
DEFAULT_CERTS_ROOT = Path(os.environ.get("AGENT_HUB_CERTS_ROOT") or Path.home())


def _certs_bundle_path(certs_root: Path) -> Path:
    """证书 bundle 的固定位置（与任务 cwd 无关）。"""
    return certs_root / ".agent-hub" / "certs" / "windows-roots.pem"


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


def codex_transport_environment(
    cwd: Path, *, certs_root: Path | None = None
) -> dict[str, str]:
    environment = dict(os.environ)
    if os.name != "nt" or environment.get("CODEX_CA_CERTIFICATE"):
        return environment

    # P0-03：证书缓存固定在项目自有目录（默认用户级 .agent-hub），
    # 绝不写入任务声明的 cwd。
    root = certs_root if certs_root is not None else DEFAULT_CERTS_ROOT
    bundle = _certs_bundle_path(root)
    if not bundle.is_file():
        export_windows_roots(bundle)
    environment["CODEX_CA_CERTIFICATE"] = str(bundle.resolve())
    return environment
