"""Regression fixture for the original saved AI response."""
import json
import pytest
from pathlib import Path

from map.functions import ai_request, parse_ideas_content

TEST_DIR = Path(__file__).resolve().parent


@pytest.mark.parametrize('headers', ['prompt,prompt', 'prompt, Prompt ', 'prompt,mode,MODE'])
def test_csv_duplicate_headers_are_rejected(headers):
    with pytest.raises(ValueError, match='duplicate column'):
        parse_ideas_content(headers + '\nPopulation,text,number\n', 'ideas.csv')


def test_saved_ai_response():
    answer = (TEST_DIR / '_ai_request.txt').read_text(encoding='utf-8')
    expected = json.loads((TEST_DIR / 'ai_request.txt').read_text(encoding='utf-8'))
    assert ai_request(prompt='test', mode='test', ai_model='test', _ai_answer=answer) == expected
