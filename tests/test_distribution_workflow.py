import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
README = ROOT / "README.md"
README_EN = ROOT / "README.en.md"
PUBLIC_DOCS = (
    README,
    README_EN,
    ROOT / "skills" / "supertoken-gpt-image-2" / "SKILL.md",
    ROOT / "skills" / "supertoken-gpt-image-2" / "references"
    / "gpt-image-2-api.md",
)
REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/generate_image.py",
    "scripts/setup.py",
    "scripts/supertoken_api.py",
    "scripts/supertoken_config.py",
    "scripts/supertoken_image.py",
    "references/gpt-image-2-api.md",
)
VIDEO_SKILL_DIR = ROOT / "skills" / "supertoken-video-generation"
VIDEO_REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/video-api.md",
    "scripts/setup.py",
    "scripts/supertoken_video.py",
    "scripts/supertoken_video_api.py",
    "scripts/supertoken_video_config.py",
)


class DistributionWorkflowTests(unittest.TestCase):
    def test_video_skill_docs_setup_and_ci_distribution_contract(self):
        self.assertTrue(
            all((VIDEO_SKILL_DIR / relative).is_file() for relative in VIDEO_REQUIRED_SKILL_FILES)
        )

        chinese = README.read_text(encoding="utf-8")
        english = README_EN.read_text(encoding="utf-8")
        skill = (VIDEO_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        reference = (VIDEO_SKILL_DIR / "references" / "video-api.md").read_text(
            encoding="utf-8"
        )
        setup = (VIDEO_SKILL_DIR / "scripts" / "setup.py").read_text(
            encoding="utf-8"
        )
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for text in (chinese, english):
            self.assertIn("supertoken-video-generation", text)
            self.assertIn("scripts/supertoken_video.py models --all", text)
            self.assertIn("--duration 4", text)
        for value in (
            "SUPERTOKEN_API_KEY",
            "SUPERTOKEN_RESOURCE_API_KEY",
            "--image",
            "--video",
            "--audio",
            "references/video-api.md",
        ):
            with self.subTest(value=value):
                self.assertIn(value, skill)
        for value in (
            "GET /v1/models",
            "POST /v1/video/tasks",
            "--reference-mode",
            "--wait-timeout",
            "首次状态查询立即执行",
            "初始轮询间隔为 2 秒",
            "Retry-After",
            "Adobe",
            "Leonardo",
            "url_auth",
            "临时 HTTPS",
        ):
            with self.subTest(reference=value):
                self.assertIn(value, reference)
        for text in (chinese, english, skill, reference):
            with self.subTest(authoritative_selection=text[:20]):
                self.assertIn("models --all", text)
        self.assertIn("getpass.getpass", setup)
        self.assertIn("--with-resource-key", setup)
        self.assertNotIn("--api-key", setup)
        self.assertNotIn("--resource-api-key", setup)
        for relative in VIDEO_REQUIRED_SKILL_FILES:
            with self.subTest(ci_relative=relative):
                self.assertIn(relative, workflow)
        self.assertIn('"supertoken-video-generation"', workflow)

    def test_video_reference_covers_cli_handoff_and_parameter_contracts(self):
        skill = (VIDEO_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        reference = (VIDEO_SKILL_DIR / "references" / "video-api.md").read_text(
            encoding="utf-8"
        )

        for value in (
            "`task_id`",
            "`task <task-id>`",
            "`upload --file <path> --kind image|video|audio`",
        ):
            with self.subTest(skill=value):
                self.assertIn(value, skill)
        for value in (
            "`--wait` 与 `--output` 必须同时提供",
            "恰好一个视频结果",
            "512 MiB",
            "`input.image`",
            "`input.reference_images[]`",
            "Kling Omni 与 Veo Standard",
            "MiniMax H3 的 `images`",
            "`adobe-kling-3.0(?:-omni)?-(720p|1080p)`",
            "`adobe-veo-3.1-(standard|fast)-(720p|1080p)`",
            "`adobe-seedance-2.0-(480p|720p)`",
            "`leonardo-seedance-2.0(?:-fast)?-[A-Za-z0-9]+`",
            "`leonardo-seedance-2.5-(480p|720p)`",
            "`leonardo-minimax-h3-1440p`",
            "`--base-url <https://...>`",
            "`task <task_id>`",
            "`upload --file <path> --kind image|video|audio`",
            "JSON 对象",
            "1-255 个可打印 ASCII 字符",
            "无查询参数或片段",
        ):
            with self.subTest(reference=value):
                self.assertIn(value, reference)

    def test_video_docs_keep_wait_audio_and_result_auth_contracts(self):
        skill = (VIDEO_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        reference = (VIDEO_SKILL_DIR / "references" / "video-api.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("只有同时传入 `--wait --output` 才会轮询并下载", skill)
        self.assertIn("Leonardo Seedance 2.0 的音频不能单独使用", reference)
        self.assertIn("`url_auth` 省略、为 `null` 或 `none`", reference)
        self.assertIn("`resource_api_key`", reference)

    def test_root_readmes_link_to_each_other_and_keep_quick_start_near_top(self):
        chinese = README.read_text(encoding="utf-8")
        english = README_EN.read_text(encoding="utf-8")

        self.assertIn("[English](README.en.md)", chinese)
        self.assertIn("[中文](README.md)", english)
        self.assertIn("## 快速开始", "\n".join(chinese.splitlines()[:60]))
        self.assertIn(
            "npx --yes skills@1.5.19 add", "\n".join(chinese.splitlines()[:60])
        )

    def test_root_readmes_include_video_upgrade_commands(self):
        for path in (README, README_EN):
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn(
                    "npx --yes skills@1.5.19 update supertoken-video-generation",
                    text,
                )
                self.assertIn(
                    "npx --yes skills@1.5.19 update -g supertoken-video-generation",
                    text,
                )
                self.assertIn(
                    "npx --yes skills@1.5.19 update -p supertoken-video-generation",
                    text,
                )

    def test_wait_deadline_documentation_includes_result_downloads(self):
        self.assertIn("结果下载", README.read_text(encoding="utf-8"))
        self.assertIn("result downloads", README_EN.read_text(encoding="utf-8").lower())
        for path in PUBLIC_DOCS[2:]:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("结果下载", text)
                self.assertIn("result downloads", text.lower())

    def test_root_readmes_keep_essential_gpt_image_2_safety_notes(self):
        for path in (README, README_EN):
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("SUPERTOKEN_API_KEY", text)
                self.assertIn("SUPERTOKEN_RESOURCE_API_KEY", text)
                self.assertIn("`ak_...`", text)
                self.assertIn(
                    "IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY", text
                )
                self.assertIn("export SUPERTOKEN_RESOURCE_API_KEY", text)
                self.assertIn("gpt-image-2-count", text)
                self.assertIn("gpt-image-2", text)
                self.assertIn("POST", text)
                self.assertIn("Webhook", text)
                self.assertIn("Base64", text)

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
        self.assertIn('assert entry.get("ref") == expected_ref, entry', text)
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

    def test_distribution_upgrades_global_shared_v01_install(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('v01-global-home', text)
        self.assertIn('--agent codex claude-code', text)
        self.assertIn('--global', text)
        self.assertIn('skills@1.5.19 update -g -y supertoken-gpt-image-2', text)
        self.assertIn('.agents/.skill-lock.json', text)
        self.assertIn('global-upgraded-skill-files.sha256', text)
        self.assertIn('test -L "$global_claude_dir"', text)

    def test_ordinary_updates_preserve_pinned_tag_and_commit_installs(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("v01_skill_files=(", text)
        self.assertIn("assert_v01_skill_files()", text)
        self.assertIn(
            'git -C "$GITHUB_WORKSPACE" show '
            '"v0.1.0:skills/supertoken-gpt-image-2/$path"',
            text,
        )

        project_start = text.index('npx --yes skills@1.5.19 add "supertoken2026/skills#v0.1.0"')
        project_retarget = text.index(
            'set_github_lock_ref "$upgrade_project/skills-lock.json" "$CANDIDATE_REF"',
            project_start,
        )
        project_pinned_update = text.index(
            'npx --yes skills@1.5.19 update -p -y supertoken-gpt-image-2',
            project_start,
        )
        self.assertLess(project_pinned_update, project_retarget)
        self.assertIn(
            'assert_github_lock_identity "$upgrade_project/skills-lock.json" "v0.1.0"',
            text[project_start:project_retarget],
        )
        self.assertGreaterEqual(
            text[project_start:project_retarget].count(
                'assert_v01_skill_files "$upgrade_skill_dir"'
            ),
            2,
        )

        commit_resolution = (
            'v01_global_commit="$(git -C "$GITHUB_WORKSPACE" '
            "rev-parse 'v0.1.0^{commit}')\""
        )
        self.assertIn(commit_resolution, text)
        self.assertLess(
            text.index(commit_resolution),
            text.index('cd "$upgrade_project"'),
        )
        global_start = text.index(commit_resolution)
        global_install = text.index('"supertoken2026/skills#v0.1.0"', global_start)
        global_commit_pin = text.index(
            'set_github_lock_ref "$global_lock" "$v01_global_commit"', global_install
        )
        global_retarget = text.index(
            'set_github_lock_ref "$global_lock" "$CANDIDATE_REF"', global_commit_pin
        )
        global_pinned_update = text.index(
            'HOME="$v01_global_home" npx --yes skills@1.5.19 update -g -y \\\n            supertoken-gpt-image-2',
            global_commit_pin,
        )
        self.assertNotIn('"supertoken2026/skills#$v01_global_commit"', text)
        self.assertLess(global_install, global_commit_pin)
        self.assertLess(global_commit_pin, global_pinned_update)
        self.assertLess(global_pinned_update, global_retarget)
        self.assertIn(
            'assert_github_lock_identity "$global_lock" "v0.1.0"',
            text[global_install:global_commit_pin],
        )
        self.assertIn(
            'assert_v01_skill_files "$global_upgrade_skill_dir"',
            text[global_install:global_commit_pin],
        )
        self.assertIn(
            'assert_github_lock_identity "$global_lock" "$v01_global_commit"',
            text[global_commit_pin:global_retarget],
        )
        self.assertIn(
            'assert_v01_skill_files "$global_upgrade_skill_dir"',
            text[global_pinned_update:global_retarget],
        )
        self.assertIn(
            'test "$(realpath "$global_claude_dir")" = "$global_upgrade_skill_dir"',
            text[global_commit_pin:global_retarget],
        )

    def test_ubuntu_hash_helper_precedes_and_verifies_fresh_installs(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        distribution = text[text.index("  distribution:"):]
        loop_start = distribution.index("for profile in codex claude-code shared")
        loop_end = distribution.index("          done", loop_start)
        local_loop = distribution[loop_start:loop_end]

        self.assertLess(distribution.index("required_skill_files=("), loop_start)
        self.assertLess(distribution.index("hash_required_skill_files()"), loop_start)
        self.assertIn('hash_required_skill_files "$expected"', local_loop)
        self.assertIn('diff -u "$RUNNER_TEMP/candidate-skill-files.sha256"', local_loop)
        self.assertLess(
            local_loop.index('diff -u "$RUNNER_TEMP/candidate-skill-files.sha256"'),
            local_loop.index('python "$expected/scripts/supertoken_image.py" --help'),
        )

    def test_v01_upgrades_use_event_aware_refs_and_verify_lock_identity(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(
            "CANDIDATE_REF: ${{ github.event_name == 'pull_request' && "
            "github.event.pull_request.head.sha || '' }}",
            text,
        )
        self.assertIn('target_ref = os.environ["TARGET_REF"] or None', text)
        self.assertIn('local target_ref="$2"', text)
        self.assertIn('candidate_update_ref="$CANDIDATE_REF"', text)
        self.assertIn('candidate_update_ref="$GITHUB_HEAD_REF"', text)
        self.assertIn('set_github_lock_ref "$upgrade_project/skills-lock.json" "$candidate_update_ref"', text)
        self.assertIn('set_github_lock_ref "$global_lock" "$candidate_update_ref"', text)
        self.assertIn('assert entry["source"] == "supertoken2026/skills", entry', text)
        self.assertIn('assert entry["sourceType"] == "github", entry', text)
        self.assertIn(
            'assert entry["skillPath"] == '
            '"skills/supertoken-gpt-image-2/SKILL.md", entry',
            text,
        )
        self.assertIn('assert entry.get("ref") == expected_ref, entry', text)
        self.assertGreaterEqual(text.count('assert_github_lock_identity '), 8)

    def test_project_and_global_v01_upgrades_share_complete_cli_smoke(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("smoke_upgraded_cli()", text)
        self.assertIn('smoke_upgraded_cli "$upgrade_skill_dir"', text)
        self.assertIn('smoke_upgraded_cli "$global_upgrade_skill_dir"', text)
        smoke = text[
            text.index("smoke_upgraded_cli()"):
            text.index("upgrade_project=", text.index("smoke_upgraded_cli()"))
        ]
        for command in ("--help", "generate --help", "edit --help", "wait --help"):
            self.assertIn(command, smoke)
        self.assertIn("legacy timeout delegation smoke passed", smoke)

    def test_platform_matrix_smokes_local_shared_install(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        python_job = text[text.index("  python-tests:"):text.index("  distribution:")]
        self.assertIn('uses: actions/setup-node@v7', python_job)
        self.assertIn('Smoke local shared install', python_job)
        self.assertIn('"--agent", "codex", "claude-code"', python_job)
        for path in REQUIRED_SKILL_FILES:
            self.assertIn(repr(path), python_job)

    def test_update_smokes_restore_every_required_installed_file(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("hash_required_skill_files()", text)
        self.assertIn("required_skill_files=(", text)
        hash_function = text[
            text.index("required_skill_files=("):
            text.index("upgrade_project=", text.index("required_skill_files=("))
        ]
        for path in REQUIRED_SKILL_FILES:
            self.assertIn(path, hash_function)

    def test_upgrade_docs_distinguish_tracking_and_pinned_refs(self):
        chinese = README.read_text(encoding="utf-8")
        english = README_EN.read_text(encoding="utf-8")

        self.assertIn("从默认分支安装", chinese)
        self.assertIn("未指定 `#ref`", chinese)
        self.assertIn("可以正常更新", chinese)
        self.assertIn("`#v0.1.0`", chinese)
        self.assertIn("固定在该 ref", chinese)
        self.assertIn("installed from the default branch", english)
        self.assertIn("unversioned", english.lower())
        self.assertIn("update normally", english)
        self.assertIn("remains pinned to that ref", english)

    def test_readme_skills_cli_commands_use_the_pinned_version(self):
        commands = [
            line.lstrip()
            for path in (README, README_EN)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("npx ")
        ]

        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(command.startswith("npx --yes skills@1.5.19 "))

        install_commands = (
            "npx --yes skills@1.5.19 add supertoken2026/skills "
            "--skill supertoken-gpt-image-2 --agent codex --global",
            "npx --yes skills@1.5.19 add supertoken2026/skills "
            "--skill supertoken-gpt-image-2 --agent codex claude-code --global",
        )
        for path in (README, README_EN):
            text = path.read_text(encoding="utf-8")
            for command in install_commands:
                with self.subTest(path=path, command=command):
                    self.assertIn(command, text)


if __name__ == "__main__":
    unittest.main()
