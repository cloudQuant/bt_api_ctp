"""离线契约测试：cleaner.yaml 的加载、默认值解析与 fail-closed 校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bt_api_ctp.cleaner.config import ConfigError, load_config, validate_config


def _payload(**overrides):
    base = {
        "tick_root": "ctp_data",
        "pull": {
            "hosts": [
                {
                    "name": "server-a",
                    "backend": "rsync",
                    "host": "user@10.0.0.1",
                    "remote_data_root": "/data/tick",
                }
            ]
        },
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "cleaner.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


class TestValidConfig:
    def test_defaults_resolve_against_base_dir(self, tmp_path):
        config = load_config(_write(tmp_path, _payload()), base_dir=tmp_path)

        assert config.tick_root == tmp_path / "ctp_data"
        assert config.kline_root == tmp_path / "ctp_data" / "kline"
        assert config.staging_root == tmp_path / "ctp_data" / "cleaner" / "staging"
        assert config.manifest_path == tmp_path / "ctp_data" / "cleaner" / "manifest.json"
        assert config.report_dir == tmp_path / "ctp_data" / "cleaner" / "reports"
        assert config.lock_path == tmp_path / "ctp_data" / "cleaner" / "cleaner.lock"
        assert config.periods == (1, 5, 15)
        assert config.include_options is True
        assert config.delete_remote_after_verify is True
        assert config.holidays_file is None

    def test_explicit_paths_and_flags(self, tmp_path):
        payload = _payload(
            kline_root="bars",
            pull={
                "delete_remote_after_verify": False,
                "staging_root": "scratch",
                "hosts": [
                    {
                        "name": "win",
                        "backend": "sftp",
                        "host": "u@1.2.3.4",
                        "remote_data_root": "D:/tick",
                    }
                ],
            },
            kline={"periods": [1, 5], "include_options": False},
            calendar={"holidays_file": "holidays.json"},
            logging={"dir": "logs", "file": False},
        )

        config = load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert config.kline_root == tmp_path / "bars"
        assert config.staging_root == tmp_path / "scratch"
        assert config.delete_remote_after_verify is False
        assert config.periods == (1, 5)
        assert config.include_options is False
        assert config.holidays_file == tmp_path / "holidays.json"
        assert config.log_to_file is False
        assert config.hosts[0].backend == "sftp"
        assert config.hosts[0].port == 22

    def test_local_backend_does_not_need_host(self, tmp_path):
        payload = _payload(
            pull={
                "hosts": [{"name": "mirror", "backend": "local", "remote_data_root": "/mnt/tick"}]
            }
        )

        config = load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert config.hosts[0].backend == "local"
        assert config.hosts[0].host == ""


class TestFailClosedValidation:
    def test_missing_tick_root(self):
        assert "tick_root is required" in validate_config({"pull": {"hosts": []}})

    def test_unknown_top_level_key_is_rejected(self):
        problems = validate_config(_payload(delete_remote_after_verify=True))

        assert any("unknown key in config" in problem for problem in problems)

    def test_unknown_pull_key_is_rejected(self):
        """A typo on the delete guard must fail, not silently keep the default."""
        payload = _payload()
        payload["pull"]["delete_remote_after_verifiy"] = False

        problems = validate_config(payload)

        assert any("delete_remote_after_verifiy" in problem for problem in problems)

    def test_unknown_host_key_is_rejected(self):
        payload = _payload()
        payload["pull"]["hosts"][0]["remote_root"] = "/data/tick"

        assert any("remote_root" in problem for problem in validate_config(payload))

    def test_unknown_backend_is_rejected(self):
        payload = _payload()
        payload["pull"]["hosts"][0]["backend"] = "ftp"

        assert any("backend must be one of" in problem for problem in validate_config(payload))

    def test_rsync_requires_host(self):
        payload = _payload()
        del payload["pull"]["hosts"][0]["host"]

        assert any(".host is required" in problem for problem in validate_config(payload))

    def test_duplicate_host_names_are_rejected(self):
        payload = _payload()
        payload["pull"]["hosts"].append(dict(payload["pull"]["hosts"][0]))

        assert any("duplicate host name" in problem for problem in validate_config(payload))

    def test_missing_remote_data_root_is_rejected(self):
        payload = _payload()
        del payload["pull"]["hosts"][0]["remote_data_root"]

        assert any(
            "remote_data_root is required" in problem for problem in validate_config(payload)
        )

    def test_empty_hosts_is_rejected(self):
        assert any("at least one host" in problem for problem in validate_config(_payload(pull={})))

    def test_unsupported_period_is_rejected(self):
        assert any(
            "unsupported period" in problem
            for problem in validate_config(_payload(kline={"periods": [1, 30]}))
        )

    def test_non_boolean_delete_flag_is_rejected(self):
        payload = _payload()
        payload["pull"]["delete_remote_after_verify"] = "yes"

        assert any("must be a boolean" in problem for problem in validate_config(payload))

    def test_invalid_port_is_rejected(self):
        payload = _payload()
        payload["pull"]["hosts"][0]["port"] = 0

        assert any("port must be an integer" in problem for problem in validate_config(payload))

    def test_load_raises_with_all_problems(self, tmp_path):
        payload = _payload(tick_root=None)
        payload["pull"]["hosts"][0]["backend"] = "ftp"

        with pytest.raises(ConfigError) as error:
            load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert len(error.value.problems) >= 2

    def test_absolute_paths_are_kept(self, tmp_path):
        payload = _payload(tick_root=str(tmp_path / "elsewhere"))

        config = load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert config.tick_root == tmp_path / "elsewhere"


class TestLocalHostCannotOverlapTickRoot:
    """local 后端 + 与 tick_root 重叠的路径会让 reclaim 删掉权威库，必须拒绝。"""

    def _local_payload(self, tick_root: str, remote_root: str):
        return {
            "tick_root": tick_root,
            "pull": {
                "hosts": [{"name": "mirror", "backend": "local", "remote_data_root": remote_root}]
            },
        }

    def test_same_directory_is_rejected(self, tmp_path):
        payload = self._local_payload("ctp_data", "ctp_data")

        with pytest.raises(ConfigError) as error:
            load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert any("overlaps tick_root" in problem for problem in error.value.problems)

    def test_remote_inside_tick_root_is_rejected(self, tmp_path):
        payload = self._local_payload("ctp_data", "ctp_data/mirror")

        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, payload), base_dir=tmp_path)

    def test_ancestor_of_tick_root_is_rejected(self, tmp_path):
        payload = self._local_payload("ctp_data", ".")

        with pytest.raises(ConfigError):
            load_config(_write(tmp_path, payload), base_dir=tmp_path)

    def test_sibling_directory_is_allowed(self, tmp_path):
        payload = self._local_payload("ctp_data", "mirror/tick")

        config = load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert config.tick_root == tmp_path / "ctp_data"

    def test_rsync_remote_path_is_not_compared_to_local_paths(self, tmp_path):
        payload = {
            "tick_root": "ctp_data",
            "pull": {
                "hosts": [
                    {
                        "name": "linux",
                        "backend": "rsync",
                        "host": "u@10.0.0.1",
                        "remote_data_root": "ctp_data",
                    }
                ]
            },
        }

        config = load_config(_write(tmp_path, payload), base_dir=tmp_path)

        assert config.hosts[0].backend == "rsync"
