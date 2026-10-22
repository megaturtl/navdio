"""Apply plugin settings through Navidrome's supported CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    executable, server_config, plugin_config, username, libraries = sys.argv[1:]
    config = json.loads(Path(plugin_config).read_text())
    config["token"] = (
        (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "event-token").read_text().strip()
    )
    command = [executable, "--nobanner", "--configfile", server_config]
    environment = {**os.environ, "ND_LOGLEVEL": "error"}

    def run_cli(arguments, data=None):
        result = subprocess.run(
            command + arguments, input=data, text=True, capture_output=True, env=environment
        )
        if result.returncode:
            sys.exit(
                "Navdio plugin setup failed. Create the configured Navidrome user and libraries "
                "first, then restart Navidrome."
            )
        return result.stdout

    # Navidrome 0.64.2 stores --users verbatim but checks internal user IDs.
    # Its CLI help incorrectly asks for usernames.
    try:
        users = json.loads(run_cli(["user", "list", "--format", "json"]))
        user_id = next(user["id"] for user in users if user["username"] == username)
    except (ValueError, KeyError, TypeError, StopIteration):
        sys.exit("Navdio plugin setup could not resolve the configured Navidrome user.")

    run_cli(["plugin", "rescan"])
    run_cli(
        [
            "plugin",
            "edit",
            "navdio",
            "--config-file",
            "-",
            "--users",
            json.dumps([user_id]),
            "--libraries",
            libraries,
            "--no-write-access",
        ],
        json.dumps(config),
    )
    run_cli(["plugin", "enable", "navdio"])


if __name__ == "__main__":
    main()
