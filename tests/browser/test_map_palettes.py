"""Exercise palette selection and request ordering in the actual map UI."""
import base64
import io
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright


pytestmark = pytest.mark.browser
VALUES = {'ukraine': 'Alpha', 'poland': 'Beta'}


@pytest.fixture
def map_page(web):
    with sync_playwright() as playwright:
        chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        kwargs = {'executable_path': str(chrome)} if chrome.exists() else {}
        browser = playwright.chromium.launch(headless=True, **kwargs)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route_request(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname == 'automap.test':
                response = web.app.test_client().open(
                    url.path, method=request.method, data=request.post_data,
                    content_type=request.headers.get('content-type'))
                route.fulfill(status=response.status_code, body=response.data,
                              content_type=response.content_type)
            elif url.hostname == 'cdn.tailwindcss.com':
                route.fulfill(content_type='application/javascript', body='window.tailwind = {};')
            else:
                route.fulfill(content_type='text/css', body='')

        page.route('**/*', route_request)
        try:
            page.goto('http://automap.test/')
            page.add_style_tag(content='.hidden {display:none!important}')
            page.evaluate('''({analysis, values}) => {
                applyAnalysisToFillingStudio(analysis, values, false);
                openFillingStudio();
            }''', {'analysis': web._build_analysis_from_ai(
                VALUES, mode='text', palette_theme='heatmap'), 'values': VALUES})
            yield page
            assert not errors, errors
        finally:
            browser.close()


def test_auto_preview_is_not_reused_as_manual_generation_rules(map_page):
    page = map_page
    state = page.evaluate('getFormState()')
    assert state['auto_style'] is True
    assert 'filling_txt' not in state and 'filling_num' not in state

    page.evaluate("updateTxtRule(0, 'hex', '#123456')")
    state = page.evaluate('getFormState()')
    assert state['auto_style'] is False
    assert state['filling_txt']['Alpha'] == [18, 52, 86, 255]

    page.select_option('#fillingPaletteTheme', 'oceanic')
    page.wait_for_function("lastMapAnalysis.resolved_theme === 'oceanic' && !paletteAnalysisPending")
    state = page.evaluate('getFormState()')
    assert state['auto_style'] is True
    assert 'filling_txt' not in state
    assert set(page.evaluate('fillingTxtRules.map(rule => rule.hex)')) == {'#38bdf8', '#0ea5e9'}

    # Apply uses the freshly calculated rules even though generation uses Auto.
    payloads = []

    def repaint(route):
        payloads.append(route.request.post_data_json)
        route.fulfill(status=500, json={'status': 'error', 'error': 'Stop after inspecting payload'})

    page.route('**/api/restyle_map', repaint)
    page.click('#restyleCurrentMapBtn')
    page.wait_for_function("!document.getElementById('restyleCurrentMapBtn').disabled")
    assert len(payloads) == 1
    assert payloads[0]['auto_style'] is False
    assert set(tuple(c) for c in payloads[0]['filling_txt'].values()) == {
        (56, 189, 248, 255), (14, 165, 233, 255)}


def test_latest_theme_wins_when_responses_arrive_out_of_order(map_page, web):
    page = map_page
    page.evaluate('''() => {
        window.pendingPalettes = [];
        const originalFetch = window.fetch;
        window.fetch = (url, options) => url === '/api/analyze_map'
            ? new Promise(resolve => pendingPalettes.push(resolve))
            : originalFetch(url, options);
    }''')
    page.select_option('#fillingPaletteTheme', 'oceanic')
    assert page.locator('#restyleCurrentMapBtn').is_disabled()
    page.evaluate('restyleCurrentMapFromStudio()')
    assert 'Wait for palette analysis' in page.locator('#toastMsg').inner_text()
    page.select_option('#fillingPaletteTheme', 'festive')
    assert page.evaluate('pendingPalettes.length') == 2

    def respond(index, theme):
        page.evaluate('''({index, payload}) => {
            pendingPalettes[index](new Response(JSON.stringify(payload), {
                status: 200, headers: {'Content-Type': 'application/json'}
            }));
        }''', {'index': index, 'payload': {'status': 'success', 'map_values': VALUES,
            'analysis': web._build_analysis_from_ai(VALUES, mode='text', palette_theme=theme)}})

    respond(1, 'festive')
    page.wait_for_function("lastMapAnalysis.resolved_theme === 'festive' && !paletteAnalysisPending")
    respond(0, 'oceanic')
    # Resolve microtasks after the stale JSON response has been handled.
    page.evaluate('() => new Promise(resolve => setTimeout(resolve, 0))')
    assert page.evaluate('lastMapAnalysis.resolved_theme') == 'festive'
    assert set(page.evaluate('fillingTxtRules.map(rule => rule.hex)')) == {'#dc2626', '#16a34a'}
    assert page.locator('#restyleCurrentMapBtn').is_enabled()


def test_failed_theme_analysis_cannot_apply_old_palette(map_page):
    page = map_page
    page.route('**/api/analyze_map', lambda route: route.fulfill(
        status=500, json={'status': 'error', 'error': 'Analysis unavailable'}))
    page.select_option('#fillingPaletteTheme', 'oceanic')
    page.wait_for_function('!paletteAnalysisPending && !paletteAnalysisReady')
    assert page.locator('#restyleCurrentMapBtn').is_disabled()
    page.evaluate('restyleCurrentMapFromStudio()')
    assert 'retry Detect' in page.locator('#toastMsg').inner_text()

    # Explicit manual editing is still available after an analysis failure.
    page.evaluate("updateTxtRule(0, 'hex', '#123456')")
    assert page.locator('#restyleCurrentMapBtn').is_enabled()
    assert page.evaluate('getFormState().auto_style') is False


@pytest.mark.parametrize('status', [200, 500])
def test_repaint_shows_busy_state_until_image_or_error_is_ready(map_page, status):
    page = map_page
    pending = []
    page.route('**/api/restyle_map', lambda route: pending.append(route))
    page.click('#restyleCurrentMapBtn')
    page.wait_for_function('mapRepaintPending')
    assert page.locator('#mapRepaintStatus').is_visible()
    assert page.locator('#mapRepaintButtonLabel').inner_text() == 'Re-painting…'
    assert page.locator('#restyleCurrentMapBtn').get_attribute('aria-busy') == 'true'
    assert page.locator('#restyleCurrentMapBtn').is_disabled()
    assert page.locator('#fillingPaletteTheme').is_disabled()
    page.evaluate('restyleCurrentMapFromStudio()')
    assert len(pending) == 1

    if status == 200:
        png = io.BytesIO()
        Image.new('RGB', (2, 2), 'green').save(png, format='PNG')
        payload = {'status': 'success', 'image_url': 'data:image/png;base64,' +
                   base64.b64encode(png.getvalue()).decode('ascii')}
    else:
        payload = {'status': 'error', 'error': 'Could not render map'}
    pending[0].fulfill(status=status, json=payload)
    page.wait_for_function('!mapRepaintPending')
    assert page.locator('#mapRepaintStatus').is_hidden()
    assert page.locator('#restyleCurrentMapBtn').get_attribute('aria-busy') == 'false'
    assert page.locator('#fillingPaletteTheme').is_enabled()
    assert page.locator('#mapRepaintButtonLabel').inner_text() == 'Apply & Re-Paint Current Map'
    if status == 200:
        assert page.locator('#serverMapImage').is_visible()
        page.wait_for_function("async () => !!(await StudioStorage.getAsset('latest-map'))?.file")
        assert page.evaluate("async () => (await StudioStorage.getAsset('latest-map')).file.size") == len(png.getvalue())
    else:
        assert page.locator('#restyleCurrentMapBtn').is_enabled()
        assert 'Could not render map' in page.locator('#toastMsg').inner_text()


def test_new_palette_is_selectable_and_analyzed(map_page):
    page = map_page
    for label in ('Greens', 'Blues', 'Reds', 'Oranges', 'Yellows', 'Purples', 'Teals', 'Pinks', 'Earth Tones'):
        assert page.locator('#fillingPaletteTheme option', has_text=label).count() == 1
    page.select_option('#fillingPaletteTheme', 'greens')
    page.wait_for_function("lastMapAnalysis.resolved_theme === 'greens' && !paletteAnalysisPending")
    assert set(page.evaluate('fillingTxtRules.map(rule => rule.hex)')) == {'#bbf7d0', '#86efac'}


def test_selected_palette_survives_reload_and_is_used_for_next_generation(map_page):
    page = map_page
    page.select_option('#fillingPaletteTheme', 'greens')
    page.wait_for_function("!paletteAnalysisPending && lastMapAnalysis.resolved_theme === 'greens'")
    page.reload()
    assert page.locator('#fillingPaletteTheme').input_value() == 'greens'
    assert page.locator('#mapPaletteThemeSelect').input_value() == 'greens'
    state = page.evaluate('getFormState()')
    assert state['palette_theme'] == 'greens'
    assert state['auto_style'] is True
    assert 'filling_txt' not in state


def test_removed_saved_palette_falls_back_to_auto(map_page):
    page = map_page
    page.evaluate("localStorage.setItem(MAP_PALETTE_STORAGE_KEY, 'removed-theme')")
    page.reload()
    assert page.locator('#fillingPaletteTheme').input_value() == 'auto'


def test_palette_selection_works_when_storage_is_blocked(map_page):
    page = map_page
    page.add_init_script('''
        Storage.prototype.getItem = Storage.prototype.setItem = () => {
            throw new DOMException('Storage blocked', 'SecurityError');
        };
    ''')
    page.reload()
    page.evaluate('openFillingStudio()')
    page.select_option('#fillingPaletteTheme', 'blues')
    assert page.evaluate('getFormState().palette_theme') == 'blues'


def test_main_form_theme_is_sent_on_first_generation_without_customize(map_page):
    page = map_page
    page.reload()
    page.add_style_tag(content='.hidden {display:none!important}')
    assert page.locator('#fillingStudioModal').is_hidden()
    page.select_option('#mapPaletteThemeSelect', 'greens')
    assert page.locator('#fillingPaletteTheme').input_value() == 'greens'
    page.locator('#aiTemperature').press('Home')
    assert page.locator('#aiTemperatureValue').inner_text() == '0.00'
    page.fill('#topicInput', 'Population of Europe')
    page.evaluate("document.getElementById('apiKeyInput').value = 'test-key-never-send'")
    payloads = []
    png = io.BytesIO()
    Image.new('RGB', (2, 2), 'green').save(png, format='PNG')
    image_url = 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode('ascii')

    def generate(route):
        payloads.append(route.request.post_data_json)
        route.fulfill(json={'status': 'success', 'image_url': image_url})

    page.route('**/api/generate', generate)
    page.click('#generateBtn')
    page.wait_for_function("!document.getElementById('generateBtn').disabled")
    assert len(payloads) == 1
    assert payloads[0]['palette_theme'] == 'greens'
    assert payloads[0]['temperature'] == 0
    assert payloads[0]['auto_style'] is True
    assert 'filling_txt' not in payloads[0]
    page.reload()
    assert page.locator('#mapPaletteThemeSelect').input_value() == 'greens'
    assert page.locator('#aiTemperature').input_value() == '0'
    assert page.locator('#aiTemperatureValue').inner_text() == '0.00'
    assert '--temperature 0' in page.locator('#cliCommandDisplay').inner_text()


def test_temperature_slider_updates_output_and_restores_from_history(map_page):
    page = map_page
    page.evaluate('closeFillingStudio()')
    page.locator('#aiTemperature').press('End')
    assert page.locator('#aiTemperatureValue').inner_text() == '2.00'
    assert '--temperature 2' in page.locator('#cliCommandDisplay').inner_text()
    page.evaluate('addToHistory(getFormState(), "test")')
    page.locator('#aiTemperature').press('Home')
    page.evaluate('restoreHistoryItem(0)')
    assert page.locator('#aiTemperature').input_value() == '2'
    page.reload()
    assert page.locator('#aiTemperature').input_value() == '2'


def test_batch_filename_is_literal_text_in_history_and_click_restores(map_page):
    page = map_page
    payload = '<img src=x onerror="window.historyXss=true">.png'
    page.evaluate('''filename => {
        window.historyXss = false;
        closeFillingStudio();
        addToHistory({...getFormState(), topic: 'Restored topic'}, `[batch] ${filename}`);
        addToHistory(getFormState(), 'Another run');
        document.getElementById('topicInput').value = 'Changed topic';
        switchBottomTab('history');
    }''', payload)
    history = page.locator('#historyList')
    assert history.locator('img').count() == 0
    assert history.locator('[onerror]').count() == 0
    assert history.inner_text().count(payload) == 1
    page.get_by_text(f'[batch] {payload}', exact=True).click()
    assert page.locator('#topicInput').input_value() == 'Restored topic'
    assert page.evaluate('window.historyXss') is False


def test_temperature_defaults_to_point_seven_five_and_can_be_reset(map_page):
    page = map_page
    page.evaluate('closeFillingStudio()')
    assert page.locator('#aiTemperature').input_value() == '0.75'
    assert page.locator('#aiTemperatureValue').inner_text() == '0.75'
    assert page.evaluate('getFormState().temperature') == 0.75
    assert '--temperature 0.75' in page.locator('#cliCommandDisplay').inner_text()
    page.locator('#aiTemperature').press('ArrowRight')
    assert page.locator('#aiTemperatureValue').inner_text() == '0.80'
    page.get_by_role('button', name='Reset to 0.75', exact=True).click()
    assert page.locator('#aiTemperature').input_value() == '0.75'
    page.reload()
    assert page.locator('#aiTemperatureValue').inner_text() == '0.75'


def test_api_key_and_topic_survive_reload_and_can_be_cleared(map_page):
    page = map_page
    page.evaluate('closeFillingStudio()')
    page.locator('#apiKeyInput').fill('AIza-test-browser-key')
    page.locator('#topicInput').fill('Population of European countries')
    page.select_option('#mapPaletteThemeSelect', 'oceanic')
    page.reload()
    assert page.locator('#apiKeyInput').input_value() == 'AIza-test-browser-key'
    assert page.locator('#apiKeyInput').get_attribute('type') == 'password'
    assert page.locator('#topicInput').input_value() == 'Population of European countries'
    assert page.locator('#mapPaletteThemeSelect').input_value() == 'oceanic'
    page.locator('#apiKeyInput').fill('')
    page.evaluate('clearTopicInput()')
    page.reload()
    assert page.locator('#apiKeyInput').input_value() == ''
    assert page.locator('#topicInput').input_value() == ''


def test_map_inputs_work_when_browser_storage_is_blocked(map_page):
    page = map_page
    page.add_init_script('''Object.defineProperty(window, 'localStorage', {
        get() { throw new DOMException('Storage blocked', 'SecurityError'); }
    });''')
    page.reload()
    page.locator('#apiKeyInput').fill('test-key')
    page.locator('#topicInput').fill('Test topic')
    assert page.evaluate('getFormState().api_key') == 'test-key'
    assert 'Test topic' in page.locator('#cliCommandDisplay').inner_text()
