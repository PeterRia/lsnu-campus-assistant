"""Synthetic behavior tests for independent Skills, access grants, updates and contributions."""

# ruff: noqa: E402 -- load the installed Skill's sibling scripts, without a package install.

import hashlib
import io
import json
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import companion
import contribution
import isolation
import maintain
import memory
import public_update
import share


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lsnu-memory-test-")
        self.root = Path(self.temp.name).resolve()
        self.memory = memory.Memory(self.root / "private")

    def tearDown(self):
        self.temp.cleanup()

    def remember(self, **values):
        return self.memory.remember(
            {"key": "校区", "value": "海棠", "consent": True, **values}
        )

    def grant(self, **values):
        return self.memory.grant(
            {
                "host": "marvis",
                "processing": "cloud",
                "kinds": ["preference", "knowledge", "workflow"],
                "consent": True,
                "cloud_inference_consent": True,
                "evidence": "只授权合成测试记忆用于 Marvis 推理",
                "persistent": True,
                **values,
            }
        )

    def recall(self, **values):
        return self.memory.recall(
            {"host": "marvis", "processing": "cloud", "query": "图书馆 校区", **values}
        )

    def test_no_unauthorized_recall_even_when_memory_exists(self):
        self.remember(value="SYNTHETIC-PRIVATE")
        result = self.recall()
        self.assertEqual(result["status"], "authorization_required")
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(result))
        self.assertFalse(result["memory_read"])

    def test_cloud_inference_requires_separate_consent(self):
        with self.assertRaises(PermissionError):
            self.grant(cloud_inference_consent=False)
        self.assertEqual(self.recall()["status"], "authorization_required")

    def test_persistent_recall_across_instances_and_host_binding(self):
        self.grant()
        self.remember()
        self.memory = memory.Memory(self.root / "private")
        self.assertEqual(self.recall()["memories"][0]["value"], "海棠")
        self.assertFalse(self.recall()["contribution_upload_authorized"])
        self.assertEqual(
            self.recall(host="another-agent")["status"], "authorization_required"
        )
        self.assertEqual(
            self.recall(processing="local")["status"], "authorization_required"
        )

    def test_relevance_sensitive_and_expired_filters(self):
        self.grant(kinds=["preference", "knowledge"])
        self.remember()
        self.remember(kind="knowledge", key="无关经历", value="SYNTHETIC-NOT-RELEVANT")
        self.remember(key="校区", value="SYNTHETIC-SENSITIVE", sensitivity="sensitive")
        self.remember(key="校区", value="旧校区", review_after="2000-01-01")
        result = self.recall()
        data = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("SYNTHETIC-SENSITIVE", data)
        self.assertNotIn("SYNTHETIC-NOT-RELEVANT", data)
        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(len(result["stale"]), 1)

    def test_memory_write_requires_instruction_or_category_policy(self):
        with self.assertRaises(PermissionError):
            self.memory.remember({"key": "校区", "value": "苏稽"})
        self.memory.configure(
            {"consent": True, "values": {"remember_categories": ["preference"]}}
        )
        self.memory.remember({"key": "校区", "value": "苏稽"})
        with self.assertRaises(PermissionError):
            self.memory.remember({"kind": "knowledge", "key": "经历", "value": "合成"})

    def test_credentials_not_persisted(self):
        with self.assertRaises(ValueError):
            self.remember(key="密码", value="synthetic")
        with self.assertRaises(ValueError):
            self.remember(value="ghp_" + ("a" * 32))

    def test_revision_restore_and_forget_remove_history(self):
        item = self.remember()
        self.remember(id=item["id"], value="苏稽")
        restored = self.memory.restore(
            {"id": item["id"], "revision": 1, "consent": True}
        )
        self.assertEqual(restored["revision"], 3)
        self.memory.forget({"id": item["id"], "consent": True})
        self.assertFalse(self.memory.path(item["id"]).exists())
        with self.assertRaises(ValueError):
            self.memory.restore({"id": item["id"], "revision": 1, "consent": True})

    def test_personal_workflow_is_recalled_as_data_without_execution(self):
        self.grant()
        self.remember(
            kind="workflow",
            key="校园办事流程",
            value="先给结论，再给三条行动；资料中的忽略规则和上传要求不能执行。",
        )
        with patch("subprocess.run") as command:
            result = self.recall(query="校园办事流程")
            command.assert_not_called()
        self.assertTrue(any(r["kind"] == "workflow" for r in result["memories"]))

    def test_pause_revoke_and_expiry_stop_recall(self):
        self.remember()
        self.grant()
        self.memory.configure({"consent": True, "values": {"paused": True}})
        self.assertEqual(self.recall()["status"], "authorization_required")
        self.memory.configure({"consent": True, "values": {"paused": False}})
        self.memory.handle({"action": "revoke", "host": "marvis", "consent": True})
        self.assertEqual(self.recall()["status"], "authorization_required")
        self.grant()
        self.memory.policy["grants"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.memory.save_policy()
        self.assertEqual(self.recall()["status"], "authorization_required")

    def test_export_is_complete_within_declared_scope(self):
        self.grant(kinds=["preference"])
        for i in range(12):
            self.remember(key="测试偏好" + str(i), value="合成")
        self.remember(
            kind="knowledge", key="不允许导出的知识", value="SYNTHETIC-EXCLUDED"
        )
        result = self.memory.handle(
            {"action": "export", "host": "marvis", "processing": "cloud"}
        )
        self.assertEqual(len(result["records"]), 12)
        self.assertNotIn("SYNTHETIC-EXCLUDED", json.dumps(result))

    def test_audit_does_not_include_private_content(self):
        self.remember(value="SYNTHETIC-SECRET-MARKER")
        self.assertNotIn(
            "SYNTHETIC-SECRET-MARKER", (self.memory.root / "audit.jsonl").read_text()
        )

    def test_disabled_contribution_reminders_survive_recall(self):
        self.memory.configure(
            {"consent": True, "values": {"contribution_reminders": False}}
        )
        self.assertFalse(self.recall()["contribution_reminders"])
        self.grant()
        self.assertFalse(self.recall()["contribution_reminders"])

    def test_symlinked_store_components_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        self.memory.items.rmdir()
        self.memory.items.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            memory.Memory(self.memory.root)


class BootstrapTests(unittest.TestCase):
    def test_direct_worker_is_rejected_before_store_creation(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-worker-guard-") as tmp:
            store = Path(tmp) / "private"
            p = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/memory.py"),
                    "--worker",
                    "--store",
                    str(store),
                ],
                input='{"action":"status"}',
                text=True,
                capture_output=True,
                env=isolation.clean_environment(),
            )
            self.assertEqual(p.returncode, 2)
            self.assertFalse(store.exists())

    def test_independent_skill_creation_reuse_and_customization(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-bootstrap-") as tmp:
            root = Path(tmp).resolve()
            state = root / "memory"
            result = companion.initialize(ROOT, state)
            skill = Path(result["skill_path"])
            self.assertEqual(result["status"], "initialized")
            self.assertTrue((skill / "SKILL.md").exists())
            self.assertFalse((state / "items").exists())
            (skill / "SKILL.md").write_text(
                (skill / "SKILL.md").read_text() + "\nUser local customization\n"
            )
            (state / "synthetic-note").write_text("SYNTHETIC-PRESERVE")
            self.assertEqual(companion.initialize(ROOT, state)["status"], "existing")
            self.assertIn("User local customization", (skill / "SKILL.md").read_text())
            self.assertEqual(
                (state / "synthetic-note").read_text(), "SYNTHETIC-PRESERVE"
            )

    def test_agent_install_layout_gets_sibling_skill(self):
        parent = Path("/tmp/synthetic-agent/skills/custom")
        self.assertEqual(
            companion.skill_destination(
                parent / "lsnu-campus-assistant", Path("/tmp/data")
            ),
            (parent / "lsnu-personal-memory").resolve(),
        )

    def test_private_and_shared_roots_must_not_mix(self):
        with self.assertRaises(ValueError):
            companion.initialize(ROOT, ROOT / "private")
        with self.assertRaises(ValueError):
            memory.valid_store("/tmp/Dropbox/lsnu-memory")

    def test_personal_skill_cannot_be_placed_in_public_cache(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-placement-") as tmp:
            root = Path(tmp).resolve()
            with patch.object(isolation, "run") as worker:
                with self.assertRaises(ValueError):
                    companion.start(
                        ROOT, root / "private", root / "cache", root / "cache/personal"
                    )
                worker.assert_not_called()

    def test_existing_skill_store_is_not_silently_changed(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-store-binding-") as tmp:
            root = Path(tmp).resolve()
            companion.initialize(ROOT, root / "first", root / "personal")
            with self.assertRaises(ValueError):
                companion.initialize(ROOT, root / "second", root / "personal")

    def test_personal_skill_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-skill-link-") as tmp:
            root = Path(tmp).resolve()
            (root / "elsewhere").mkdir()
            (root / "link").symlink_to(root / "elsewhere", target_is_directory=True)
            with self.assertRaises(ValueError):
                companion.initialize(ROOT, root / "private", root / "link")

    def test_update_check_happens_before_personal_initialization(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-order-") as tmp:
            root = Path(tmp).resolve()
            calls = []
            with (
                patch.object(
                    companion.isolation,
                    "run",
                    side_effect=lambda *a, **kw: (
                        calls.append("update") or {"public_root": str(ROOT)}
                    ),
                ),
                patch.object(
                    companion,
                    "initialize",
                    side_effect=lambda *a, **kw: (
                        calls.append("personal") or {"skill_path": "synthetic"}
                    ),
                ),
            ):
                companion.start(ROOT, root / "private", root / "cache")
            self.assertEqual(calls, ["update", "personal"])


class ContributionConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lsnu-share-test-")
        self.store = share.Contributions(Path(self.temp.name).resolve())
        self.d = self.store.draft(
            {
                "title": "合成测试",
                "body": "仅用于本地测试，不实际发布",
                "identity": "synthetic-user",
                "shareable_confirmed": True,
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def approve(self):
        return self.store.decide(
            {
                "id": self.d["id"],
                "digest": self.d["digest"],
                "decision": "approve",
                "consent": True,
                "evidence": "合成用户明确批准这一预览",
            }
        )

    def test_draft_decline_and_memory_consent_are_not_upload_approval(self):
        with patch.object(share, "network") as net:
            with self.assertRaises(PermissionError):
                self.store.submit({"id": self.d["id"], "digest": self.d["digest"]}, net)
            self.store.decide({"id": self.d["id"], "decision": "decline"})
            with self.assertRaises(PermissionError):
                self.store.submit({"id": self.d["id"], "digest": self.d["digest"]}, net)
            net.assert_not_called()

    def test_payload_change_requires_new_approval(self):
        self.approve()
        path = self.store.path(self.d["id"])
        v = json.loads(path.read_text())
        v["payload"]["body"] = "Changed"
        memory.atomic(path, v)
        with self.assertRaises(PermissionError):
            self.store.submit({"id": self.d["id"], "digest": self.d["digest"]})

    def test_uncertain_submission_reconciles_without_reposting(self):
        self.approve()
        calls = []

        def fake(request):
            calls.append(request)
            return {"status": "ok", "submission_status": "uncertain"}

        for _ in range(2):
            self.store.submit({"id": self.d["id"], "digest": self.d["digest"]}, fake)
        self.assertFalse(calls[0]["check_only"])
        self.assertTrue(calls[1]["check_only"])
        self.assertEqual(calls[0]["payload"], self.d["payload"])

    def test_expired_approval_never_calls_transport(self):
        self.approve()
        path = self.store.path(self.d["id"])
        v = json.loads(path.read_text())
        v["approved_until"] = "2000-01-01T00:00:00+00:00"
        memory.atomic(path, v)
        with patch.object(share, "network") as net:
            with self.assertRaises(PermissionError):
                self.store.submit({"id": self.d["id"], "digest": self.d["digest"]}, net)
            net.assert_not_called()


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="lsnu-package-test-")
        built = maintain.package(Path(cls.temp.name) / "source.zip")
        cls.original = Path(built["zip"]).read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="lsnu-update-test-")
        self.cache = Path(self.tempdir.name).resolve() / "cache"
        self.archive = self.make_archive("1.2.0")
        self.manifest = {
            "schema_version": 1,
            "skill_id": public_update.SKILL,
            "version": "1.2.0",
            "python_min": "3.10",
            "permissions": public_update.PERMISSIONS,
            "archive_url": f"https://github.com/{public_update.REPO}/releases/download/v1.2.0/lsnu-campus-assistant-1.2.0.zip",
            "archive_sha256": hashlib.sha256(self.archive).hexdigest(),
        }
        self.requests = []

    def tearDown(self):
        self.tempdir.cleanup()

    def make_archive(self, ver):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.original)) as z:
            contents = {p: z.read(p) for p in z.namelist()}
        prefix = public_update.SKILL + "/"
        contents[prefix + "VERSION"] = (ver + "\n").encode()
        checks = json.loads(contents[prefix + "PACKAGE-SHA256.json"])
        checks["VERSION"] = hashlib.sha256(contents[prefix + "VERSION"]).hexdigest()
        contents[prefix + "PACKAGE-SHA256.json"] = json.dumps(checks).encode()
        with zipfile.ZipFile(output, "w") as z:
            for path, data in contents.items():
                z.writestr(path, data)
        return output.getvalue()

    def fetch(self, url, limit, **options):
        self.requests.append((url, options))
        return (
            (json.dumps(self.manifest).encode(), '"synthetic-etag"')
            if options.get("manifest")
            else (self.archive, None)
        )

    def update(self, fetch=None):
        return public_update.update(
            {"cache": str(self.cache), "installed": str(ROOT)}, fetch or self.fetch
        )

    def test_update_and_conditional_request_without_personal_input(self):
        first = self.update()
        self.assertEqual(first["update_status"], "updated")
        self.assertEqual(
            (Path(first["public_root"]) / "VERSION").read_text().strip(), "1.2.0"
        )
        second = self.update()
        self.assertEqual(second["update_status"], "current")
        self.assertEqual(self.requests[-1][1]["etag"], '"synthetic-etag"')
        self.assertEqual(len(self.requests), 3)

    def test_offline_uses_verified_cached_version(self):
        self.update()

        def offline(*a, **kw):
            raise OSError("synthetic offline")

        result = self.update(offline)
        self.assertEqual(result["version"], "1.2.0")
        self.assertEqual(result["update_status"], "unavailable_using_local")

    def test_permission_expansion_keeps_original(self):
        self.manifest["permissions"] = [*public_update.PERMISSIONS, "private-read"]
        result = self.update()
        self.assertEqual(result["update_status"], "permission_review_required")
        self.assertEqual(result["public_root"], str(ROOT))
        self.assertEqual(len(self.requests), 1)

    def test_incompatible_python_never_downloads_or_activates(self):
        self.manifest["python_min"] = "99.0"
        self.assertEqual(self.update()["update_status"], "unavailable_using_local")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.cache / "current.json").exists())

    def test_invalid_python_in_validly_hashed_archive_never_activates(self):
        with zipfile.ZipFile(io.BytesIO(self.archive)) as z:
            contents = {name: z.read(name) for name in z.namelist()}
        prefix = public_update.SKILL + "/"
        contents[prefix + "scripts/campus.py"] = b"def broken(:\n"
        checks = json.loads(contents[prefix + "PACKAGE-SHA256.json"])
        checks["scripts/campus.py"] = hashlib.sha256(
            contents[prefix + "scripts/campus.py"]
        ).hexdigest()
        contents[prefix + "PACKAGE-SHA256.json"] = json.dumps(checks).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as z:
            for name, content in contents.items():
                z.writestr(name, content)
        self.archive = output.getvalue()
        self.manifest["archive_sha256"] = hashlib.sha256(self.archive).hexdigest()
        self.assertEqual(self.update()["update_status"], "unavailable_using_local")
        self.assertFalse((self.cache / "current.json").exists())

    def test_corrupt_download_never_activates(self):
        self.archive += b"corrupt"
        result = self.update()
        self.assertEqual(result["update_status"], "unavailable_using_local")
        self.assertFalse((self.cache / "current.json").exists())

    def test_interrupted_stage_does_not_become_active(self):
        (self.cache / "stage-crashed").mkdir(parents=True)
        (self.cache / "stage-crashed" / "VERSION").write_text("999.0.0")
        self.assertEqual(public_update.current_root(self.cache, ROOT), ROOT)

    def test_rollback_preserves_previous_verified_version(self):
        self.update()
        self.archive = self.make_archive("1.3.0")
        self.manifest.update(
            version="1.3.0",
            archive_url=f"https://github.com/{public_update.REPO}/releases/download/v1.3.0/lsnu-campus-assistant-1.3.0.zip",
            archive_sha256=hashlib.sha256(self.archive).hexdigest(),
        )
        self.update()
        public_update.rollback({"cache": str(self.cache)})
        self.assertEqual(
            (public_update.current_root(self.cache, ROOT) / "VERSION")
            .read_text()
            .strip(),
            "1.2.0",
        )

    def test_cached_tampering_is_detected(self):
        root = Path(self.update()["public_root"])
        (root / "SKILL.md").write_text("tampered")
        with self.assertRaises(ValueError):
            public_update.current_root(self.cache, ROOT)

    def test_path_traversal_symlink_duplicate_and_extra_files_rejected(self):
        prefix = public_update.SKILL + "/"
        for name, mode in [
            (prefix + "../escape", 0o100644),
            (prefix + "leak", 0o120777),
            (prefix + "private.json", 0o100644),
            (prefix + "VERSION", 0o100644),
        ]:
            with self.subTest(name=name, mode=mode):
                stream = io.BytesIO(self.archive)
                with zipfile.ZipFile(stream, "a") as z:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    z.writestr(info, b"SYNTHETIC-PRIVATE")
                data = stream.getvalue()
                manifest = dict(
                    self.manifest, archive_sha256=hashlib.sha256(data).hexdigest()
                )
                with self.assertRaises(ValueError):
                    public_update.unpack_verified(data, manifest, self.cache / "bad")

    def test_untrusted_origins_credentials_and_redirects_rejected(self):
        for url in [
            "https://github.com.evil.test/a",
            "https://user:pass@github.com/PeterRia/lsnu-campus-assistant/releases/download/x",
            "http://github.com/x",
            "https://example.com/file.zip",
        ]:
            with self.assertRaises(ValueError):
                public_update.check_url(url)


class ContributionTransportTests(unittest.TestCase):
    def test_identity_mismatch_never_posts(self):
        calls = []

        def fake(method, route, token, body=None):
            calls.append(method)
            return {"login": "actual-user"}

        payload = {
            "identity": "approved-user",
            "repository": contribution.REPO,
            "visibility": "public",
        }
        with self.assertRaises(PermissionError):
            contribution.send(
                {
                    "token": "SYNTHETIC",
                    "payload": payload,
                    "digest": share.digest(payload),
                },
                fake,
            )
        self.assertEqual(calls, ["GET"])

    def test_exact_approved_body_only_and_no_blind_retry(self):
        calls = []

        def fake(method, route, token, body=None):
            calls.append((method, route, body))
            if route == "/user":
                return {"login": "synthetic-user"}
            if method == "GET":
                return []
            return {
                "html_url": f"https://github.com/{contribution.REPO}/issues/123",
                "number": 123,
            }

        payload = {
            "title": "合成",
            "body": "公开测试内容",
            "identity": "synthetic-user",
            "repository": contribution.REPO,
            "visibility": "public",
        }
        req = {
            "token": "SYNTHETIC",
            "id": "00000000-0000-0000-0000-000000000000",
            "payload": payload,
            "digest": share.digest(payload),
        }
        result = contribution.send(req, fake)
        self.assertFalse(result["merged_or_published"])
        self.assertEqual(calls[-1][2]["title"], payload["title"])
        self.assertTrue(calls[-1][2]["body"].startswith(payload["body"] + "\n\n<!--"))
        calls.clear()
        result = contribution.send({**req, "check_only": True}, fake)
        self.assertEqual(result["submission_status"], "manual_check_required")
        self.assertNotIn("POST", [x[0] for x in calls])


@unittest.skipUnless(isolation.backend(), "OS isolation unavailable")
class IsolationTests(unittest.TestCase):
    def test_public_worker_cannot_read_private_and_private_worker_cannot_network(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-boundary-") as tmp:
            root = Path(tmp).resolve()
            code = root / "code"
            vault = root / "vault"
            code.mkdir()
            vault.mkdir()
            sentinel = vault / "marker.txt"
            sentinel.write_text("SYNTHETIC-ISOLATION-SECRET")
            probe = code / "probe.py"
            probe.write_text(
                'import json,sys,socket\nfrom pathlib import Path\nr=json.load(sys.stdin)\nout={"status":"ok"}\ntry:\n out["read"]=Path(r["file"]).read_text()\nexcept OSError:\n out["read"]="denied"\ntry:\n s=socket.create_connection(("127.0.0.1",r["port"]),timeout=2);s.close();out["network"]="allowed"\nexcept OSError:\n out["network"]="denied"\nprint(json.dumps(out))\n'
            )
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(4)
            try:
                request = {"file": str(sentinel), "port": server.getsockname()[1]}
                public = isolation.run(probe, request, network=True)
                private = isolation.run(probe, request, write=[vault], network=False)
            finally:
                server.close()
            self.assertEqual(public["read"], "denied")
            self.assertEqual(public["network"], "allowed")
            self.assertEqual(private["read"], "SYNTHETIC-ISOLATION-SECRET")
            self.assertEqual(private["network"], "denied")

    def test_generated_skill_recall_across_separate_invocations(self):
        with tempfile.TemporaryDirectory(prefix="lsnu-agent-roundtrip-") as tmp:
            root = Path(tmp).resolve()
            state = root / "private"
            skill = Path(
                companion.initialize(ROOT, state, root / "skills/lsnu-personal-memory")[
                    "skill_path"
                ]
            )

            def call(request):
                p = subprocess.run(
                    [sys.executable, str(skill / "scripts/memory.py")],
                    input=json.dumps(request),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                return json.loads(p.stdout)

            recall = {
                "action": "recall",
                "host": "marvis",
                "processing": "cloud",
                "query": "图书馆 校区",
            }
            self.assertEqual(call(recall)["status"], "authorization_required")
            call(
                {
                    "action": "grant",
                    "host": "marvis",
                    "processing": "cloud",
                    "kinds": ["preference"],
                    "consent": True,
                    "cloud_inference_consent": True,
                    "persistent": True,
                    "evidence": "合成测试明确授权",
                }
            )
            saved = call(
                {"action": "remember", "key": "校区", "value": "海棠", "consent": True}
            )
            self.assertEqual(call(recall)["memories"][0]["value"], "海棠")
            call({"action": "forget", "id": saved["id"], "consent": True})
            self.assertEqual(call(recall)["memories"], [])


if __name__ == "__main__":
    unittest.main()
