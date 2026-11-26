"""The system prompt steers small models away from tool calls for plain answers (seen with qwen2.5-coder:7b)."""

from wildcat_code_agent.core import loop


def test_the_system_prompt_asks_for_plain_answers_and_no_echo_commands():
    prompt = loop.SYSTEM_PROMPT
    assert "plain text" in prompt
    assert "Call a tool only when" in prompt
    assert "Never use run_command just to print or echo" in prompt


def test_a_new_messages_starts_with_the_system_prompt():
    assert loop.new_messages()[0] == {"role": "system", "content": loop.SYSTEM_PROMPT}
