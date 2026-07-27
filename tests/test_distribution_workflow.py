import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
PUBLIC_DOCS = (
    ROOT / "README.md",
    ROOT / "skills" / "supertoken-gpt-image-2" / "SKILL.md",
    ROOT / "skills" / "supertoken-gpt-image-2" / "references"
    / "gpt-image-2-api.md",
)


class DistributionWorkflowTests(unittest.TestCase):
    def test_wait_deadline_documentation_includes_result_downloads(self):
        for path in PUBLIC_DOCS:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("结果下载", text)
                self.assertIn("result downloads", text.lower())

    def test_api_reference_describes_redacted_idempotency_output(self):
        text = PUBLIC_DOCS[-1].read_text(encoding="utf-8")

        self.assertIn("脱敏后的本次 `Idempotency-Key`", text)
        self.assertIn("redacted `Idempotency-Key`", text)

    def test_local_shared_install_checks_codex_claude_symlink(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('profile in codex claude-code shared', text)
        self.assertIn('agents=(codex claude-code)', text)
        self.assertIn('test -L "$claude_dir"', text)
        self.assertIn('test "$(realpath "$claude_dir")" = "$skill_dir"', text)

    def test_distribution_upgrades_a_real_v01_install_to_candidate(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('supertoken2026/skills#v0.1.0', text)
        self.assertIn('github.event.pull_request.head.sha', text)
        self.assertIn('lock["skills"]["supertoken-gpt-image-2"]["ref"]', text)
        self.assertIn(
            'skills@1.5.19 update -p -y supertoken-gpt-image-2', text
        )
        for path in (
            "scripts/generate_image.py",
            "scripts/supertoken_image.py",
            "scripts/supertoken_api.py",
            "references/gpt-image-2-api.md",
        ):
            self.assertIn(path, text)
        self.assertIn('legacy timeout delegation smoke passed', text)
        self.assertIn(
            'assert entry["ref"] == os.environ["CANDIDATE_REF"], entry', text
        )
        self.assertIn(
            'hash_required_skill_files "$GITHUB_WORKSPACE/skills/'
            'supertoken-gpt-image-2"',
            text,
        )
        self.assertIn(
            'diff -u "$RUNNER_TEMP/candidate-skill-files.sha256" '
            '"$RUNNER_TEMP/upgraded-skill-files.sha256"',
            text,
        )

    def test_update_smokes_restore_every_required_installed_file(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("hash_required_skill_files()", text)
        for path in (
            "SKILL.md",
            "scripts/generate_image.py",
            "scripts/supertoken_image.py",
            "scripts/supertoken_api.py",
            "references/gpt-image-2-api.md",
        ):
            self.assertIn(path, text)


if __name__ == "__main__":
    unittest.main()
