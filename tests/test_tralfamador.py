import unittest
import re
from pathlib import Path

import tralfamador


class TralfamadorTests(unittest.TestCase):
    def test_normalize_same_site_links_without_personal_state(self) -> None:
        root = "https://example.org/news/"
        self.assertEqual(
            tralfamador.normalize_url_for_root("/news/story-one/?utm_source=x", root),
            "https://example.org/news/story-one/",
        )
        self.assertIsNone(tralfamador.normalize_url_for_root("https://other.example/news/story/", root))

    def test_extract_links_for_root_keeps_only_requested_site(self) -> None:
        html = """
        <a href="/news/story-one/">one</a>
        <a href="https://example.org/news/story-two/?fbclid=abc">two</a>
        <a href="https://elsewhere.example/news/story-three/">three</a>
        """
        self.assertEqual(
            tralfamador.extract_links_for_root(html, "https://example.org/news/", "https://example.org/news/"),
            [
                "https://example.org/news/story-one/",
                "https://example.org/news/story-two/",
            ],
        )

    def test_default_candidate_filter_is_generic(self) -> None:
        candidate_re = re.compile(tralfamador.DEFAULT_CANDIDATE_REGEX)
        exclude_re = re.compile(tralfamador.DEFAULT_EXCLUDE_REGEX)
        self.assertTrue(
            tralfamador.is_generic_article_candidate(
                "https://example.org/news/story-one/",
                candidate_re,
                exclude_re,
            )
        )
        self.assertFalse(
            tralfamador.is_generic_article_candidate(
                "https://example.org/tag/story-one/",
                candidate_re,
                exclude_re,
            )
        )

    def test_manifest_output_paths_are_relative_to_run_dir(self) -> None:
        out_dir = Path("/tmp/tralfamador-run")
        output = out_dir / "articles" / "2021-01" / "story.html"
        self.assertEqual(
            tralfamador.portable_output_path(out_dir, output),
            "articles/2021-01/story.html",
        )

    def test_resume_command_is_available(self) -> None:
        parser = tralfamador.build_parser()
        args = parser.parse_args(
            [
                "--out",
                "data/example",
                "resume-discovered-html",
                "--candidates",
                "data/example/discovered_links.jsonl",
            ]
        )
        self.assertEqual(args.func, tralfamador.resume_discovered_html)
        self.assertTrue(args.skip_recovered)

    def test_cli_default_delay_is_conservative_for_archive_access(self) -> None:
        parser = tralfamador.build_parser()
        args = parser.parse_args(["probe", "--root-url", "https://example.org/news/"])
        self.assertEqual(args.delay, 4.0)

    def test_cli_retries_can_be_tuned_for_bulk_resume(self) -> None:
        parser = tralfamador.build_parser()
        args = parser.parse_args(
            [
                "--retries",
                "1",
                "resume-discovered-html",
                "--candidates",
                "data/example/discovered_links.jsonl",
            ]
        )
        self.assertEqual(args.retries, 1)

    def test_resume_can_use_availability_strategy(self) -> None:
        parser = tralfamador.build_parser()
        args = parser.parse_args(
            [
                "resume-discovered-html",
                "--candidates",
                "data/example/discovered_links.jsonl",
                "--latest-strategy",
                "availability",
            ]
        )
        self.assertEqual(args.latest_strategy, "availability")


if __name__ == "__main__":
    unittest.main()
