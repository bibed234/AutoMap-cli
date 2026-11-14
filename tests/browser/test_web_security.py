"""Untrusted model responses and UI text must remain literal browser text."""
import pytest

from test_map_palettes import map_page


pytestmark = pytest.mark.browser


def test_ai_labels_and_samples_cannot_create_html(map_page, web):
    payload = '\"><img src=x onerror="window.injected=true">'
    analysis = web._build_analysis_from_ai({'ukraine': payload}, mode='text')
    map_page.evaluate('''({analysis, payload}) => {
        window.injected = false;
        applyAnalysisToFillingStudio(analysis, {ukraine: payload}, false);
    }''', {'analysis': analysis, 'payload': payload})
    assert map_page.locator('#detectedMapStatsBar img').count() == 0
    assert map_page.locator('#fillingTxtList img').count() == 0
    assert payload in map_page.locator('#detectedMapStatsBar').inner_text()
    assert map_page.locator('#fillingTxtList input[type=text]').first.input_value() == payload
    assert map_page.evaluate('window.injected') is False


def test_custom_model_is_literal_badge_text(map_page):
    payload = '<img src=x onerror="window.injected=true">'
    map_page.evaluate('''payload => {
        window.injected = false;
        document.getElementById('newModelInput').value = payload;
        addCustomModel();
    }''', payload)
    assert map_page.locator('#activeFlagsBadges img').count() == 0
    assert payload in map_page.locator('#activeFlagsBadges').inner_text()
    assert map_page.evaluate('window.injected') is False


def test_malformed_analysis_color_is_replaced_with_safe_color(map_page):
    map_page.evaluate('''() => {
        window.injected = false;
        applyAnalysisToFillingStudio({
            ui_txt_rules: [{key: 'hello', hex: '\"><img src=x onerror="window.injected=true">'}],
            ui_num_rules: [], stats: {},
        }, {ukraine: 'hello'}, false);
    }''')
    assert map_page.locator('#fillingTxtList img').count() == 0
    assert map_page.evaluate('fillingTxtRules[0].hex') == '#6366f1'
    assert map_page.evaluate('window.injected') is False
