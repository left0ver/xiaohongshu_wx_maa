import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path


def run(service, directory, java):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    credentials = directory / "credentials.json"
    if credentials.exists():
        values = json.loads(credentials.read_text())
    else:
        values = {"username": "wechat-integration", "password": secrets.token_urlsafe(24),
                  "apiKey": secrets.token_urlsafe(40), "cryptoKey": secrets.token_urlsafe(40)}
        credentials.write_text(json.dumps(values), encoding="utf-8")
        credentials.chmod(0o600)
    environment = dict(os.environ)
    environment.update({
        "SPRING_PROFILES_ACTIVE": "test", "SERVER_PORT": "11098",
        "TEST_DB_URL": "jdbc:mysql://127.0.0.1:13306/wechat_service?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=Asia/Shanghai",
        "TEST_DB_USERNAME": "root", "TEST_DB_PASSWORD": "",
        "TEST_REDIS_HOST": "127.0.0.1", "TEST_REDIS_PORT": "16379", "TEST_REDIS_PASSWORD": "", "TEST_REDIS_DATABASE": "0",
        "BOOTSTRAP_ADMIN_USERNAME": values["username"], "BOOTSTRAP_ADMIN_PASSWORD": values["password"],
        "LEAD_ENCRYPTION_KEY": values["cryptoKey"],
        "SYSTEM_SECRET_ENCRYPTION_KEY": values["cryptoKey"],
        "SYSTEM_SECRET_KEY_FILE": str(directory / "system-key"),
        "MODULE_STORAGE_DIRECTORY": str(directory / "modules"),
        "MODULE_FEATURE_CONFIG": str(service.resolve() / "modules/features.properties"),
        "CAPTURE_SCREENSHOT_DIRECTORY": str(directory / "captures"),
        "LOG_HOME": str(directory / "logs"),
    })
    with (directory / "server.log").open("a") as output:
        process = subprocess.Popen([str(java), "-jar", str(service.resolve() / "target/hk-server-1.0.0.jar")],
                                   cwd=directory, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        (directory / "server.pid").write_text(str(process.pid))
        print("独立验收服务端口：11098", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("service", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("java", type=Path)
    args = parser.parse_args()
    run(args.service, args.directory, args.java)
