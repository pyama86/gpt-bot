import os
import unittest
from unittest.mock import Mock, patch

from main import (base_url, get_github_client, get_token_with_type,
                  handle_comment_request, handle_pull_request,
                  handle_summary_request, process_issue_comment, query_ai,
                  update_issue_body)


class TestMain(unittest.TestCase):

    @patch("main.Github")
    def test_get_github_client(self, MockGithub):
        mock_client = MockGithub.return_value
        result = get_github_client("token")
        self.assertEqual(result, mock_client)
        MockGithub.assert_called_once_with(login_or_token="token", base_url=base_url())

    @patch.dict(os.environ, {"GITHUB_API": "https://custom.api/"})
    def test_base_url_custom(self):
        self.assertEqual(base_url(), "https://custom.api")

    @patch.dict(os.environ, {}, clear=True)
    def test_base_url_default(self):
        self.assertEqual(base_url(), "https://api.github.com/")

    @patch("main.GithubIntegration")
    @patch.dict(
        os.environ, {"GITHUB_APP_ID": "123", "GITHUB_APP_PRIVATE_KEY": "private_key"}
    )
    def test_get_token_with_type_app(self, MockIntegration):
        mock_integration = MockIntegration.return_value
        mock_integration.get_repo_installation.return_value.id = 12345
        mock_integration.get_access_token.return_value.token = "test_token"

        token_type, token = get_token_with_type("org", "repo")
        self.assertEqual(token_type, "Bearer")
        self.assertEqual(token, "test_token")
        MockIntegration.assert_called_once_with(
            integration_id="123", private_key="private_key", base_url=base_url()
        )
        mock_integration.get_repo_installation.assert_called_once_with("org", "repo")
        mock_integration.get_access_token.assert_called_once_with(12345)

    @patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}, clear=True)
    def test_get_token_with_type_token(self):
        token_type, token = get_token_with_type("org", "repo")
        self.assertEqual(token_type, "token")
        self.assertEqual(token, "test_token")

    @patch("main.OpenAI")
    def test_query_ai(self, MockOpenAI):
        mock_response = Mock()
        mock_choice = Mock()
        mock_choice.message.content = "Summary"
        mock_response.choices = [mock_choice]

        MockOpenAI.return_value.chat.completions.create.return_value = mock_response

        result = query_ai("Content")
        self.assertEqual(result, "Summary")

    def test_update_issue_body(self):
        mock_issue = Mock()
        mock_issue.body = (
            "Line 1\n<!-- summary start -->\nOld summary\n<!-- summary end -->\nLine 4"
        )

        update_issue_body(mock_issue, "New summary")
        mock_issue.edit.assert_called_once_with(
            body="Line 1\n<!-- summary start -->\nNew summary\n<!-- summary end -->\nLine 4"
        )

    @patch("main.get_token_with_type")
    @patch("main.get_github_client")
    def test_process_issue_comment(self, MockGithub, MockToken):
        MockToken.return_value = ("Bearer", "test_token")
        mock_issue = Mock()
        MockGithub.return_value.get_repo.return_value.get_issue.return_value = (
            mock_issue
        )

        data = {
            "repository": {
                "owner": {"login": "org"},
                "name": "repo",
                "full_name": "org/repo",
            },
            "issue": {"number": 1, "pull_request": None},
            "comment": {"body": "@gpt-bot 今北産業"},
        }

        with patch("main.handle_summary_request") as MockHandleSummary:
            process_issue_comment(data)
            MockHandleSummary.assert_called_once_with(mock_issue)

    @patch("main.query_ai")
    def test_handle_summary_request(self, MockQueryAI):
        mock_issue = Mock()
        mock_issue.body = "Some issue body"
        mock_issue.get_comments.return_value = []
        MockQueryAI.return_value = "Mocked Summary"

        handle_summary_request(mock_issue)
        mock_issue.edit.assert_called_once()
        mock_issue.create_comment.assert_called_once_with(
            "AIによる議論のサマリがIssueの本文に更新されました。"
        )
        MockQueryAI.assert_called_once()
        args, _ = MockQueryAI.call_args
        assert args[0].find("3行まとめ") > 0

    @patch("main.query_ai")
    def test_handle_comment_request(self, MockQueryAI):
        mock_issue = Mock()
        MockQueryAI.return_value = "Mocked Comment"

        data = {"comment": {"body": "@gpt-bot /comment Some comment text"}}

        handle_comment_request(data, mock_issue)
        mock_issue.create_comment.assert_called_once_with("Mocked Comment")
        MockQueryAI.assert_called_once()
        args, _ = MockQueryAI.call_args
        assert args[0].find("GitHubで生成されたIssueのコメントを入力します。") > 0

    @patch("main.tiktoken.encoding_for_model")
    @patch("main.requests.get")
    @patch("main.query_ai")
    @patch("main.get_token_with_type")
    def test_handle_pull_request(self, MockToken, MockQueryAI, MockGet, MockEncoding):
        MockToken.return_value = ("Bearer", "test_token")
        mock_issue = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"filename": "file1.py", "patch": "diff"}]
        MockGet.return_value = mock_response

        mock_encoding = Mock()
        mock_encoding.encode.return_value = [1] * 100000
        MockEncoding.return_value = mock_encoding

        data = {
            "repository": {
                "owner": {"login": "org"},
                "name": "repo",
                "full_name": "org/repo",
            },
            "issue": {
                "number": 1,
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/1"
                },
            },
            "comment": {"body": "@gpt-bot /command 提案してください"},
        }

        MockQueryAI.return_value = "Mocked PR Response"

        handle_pull_request(data, mock_issue)
        mock_issue.create_comment.assert_called_once_with("Mocked PR Response")
        MockQueryAI.assert_called_once()
        args, _ = MockQueryAI.call_args
        assert args[0].find("提案してください") > 0


if __name__ == "__main__":
    unittest.main()
