import os
import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(page_title='ヨドバシ × Keepa 価格差リサーチ', layout='wide', page_icon='🔎')
st.title('ヨドバシ × Amazon 価格差リサーチ')
st.caption('試作版：公開ページとKeepa APIを使用します。サイトのアクセス制限により取得できない場合があります。')
DEFAULT_URL = 'https://www.yodobashi.com/category/141001/141336/'


def make_session():
    s = requests.Session()
    s.headers.update({'User-Agent': 'PriceResearchPrototype/1.1 (public page research)', 'Accept': 'text/html,application/xhtml+xml'})
    retries = Retry(total=2, connect=2, read=2, backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=['GET'], respect_retry_after_header=True)
    s.mount('https://', HTTPAdapter(max_retries=retries))
    return s


def valid_category(url):
    p = urlparse(url)
    return p.scheme == 'https' and p.hostname in ('www.yodobashi.com', 'yodobashi.com') and p.path.startswith('/category/')


def parse_yodobashi(html, base_url):
    soup = BeautifulSoup(html, 'lxml')
    found = {}
    for a in soup.select('a[href*="/product/"]'):
        href = urljoin(base_url, a.get('href', ''))
        if urlparse(href).hostname not in ('www.yodobashi.com', 'yodobashi.com') or not re.search(r'/product/\d+', href):
            continue
        block = a.find_parent(['li', 'article']) or a.parent
        txt = block.get_text(' ', strip=True)
        price = re.search(r'(?:￥|¥)\s*([\d,]+)', txt)
        name = a.get_text(' ', strip=True)
        if not name or not price:
            continue
        yen = int(price.group(1).replace(',', ''))
        if yen > 0:
            found[href] = {'商品名': name[:160], 'ヨドバシ価格': yen, 'ヨドバシURL': href}
    return list(found.values())


def extract_jan(html):
    patterns = [r'(?:JANコード|JAN|EAN|GTIN)[^0-9]{0,50}(\d{13})', r'"gtin13"\s*:\s*"(\d{13})"']
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1)
    return None


def keepa_lookup(session, api_key, jan):
    response = session.get('https://api.keepa.com/product', params={
        'key': api_key, 'domain': 5, 'code': jan, 'stats': 1
    }, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    if payload.get('error'):
        raise ValueError('Keepa APIエラー: ' + str(payload['error'])[:150])
    products = payload.get('products') or []
    if not products:
        return None
    p = products[0]
    current = (p.get('stats') or {}).get('current') or []
    # Keepaの日本円価格は通常そのまま円単位（-1はデータなし）。
    # BUY_BOX_SHIPPING=18, NEW=1, AMAZON=0
    for idx, label in [(18, 'Buy Box（送料込み）'), (1, '新品出品価格'), (0, 'Amazon本体価格')]:
        if len(current) > idx and isinstance(current[idx], (int, float)) and current[idx] > 0:
            return {'ASIN': p.get('asin', ''), 'Amazon価格': int(current[idx]), '価格指標': label}
    return None


def friendly_error(e):
    if isinstance(e, requests.exceptions.Timeout):
        return '接続がタイムアウトしました。サイト側の応答遅延やサーバーからのアクセス制限が考えられます。'
    if isinstance(e, requests.exceptions.HTTPError):
        code = e.response.status_code if e.response is not None else '不明'
        return f'HTTP {code}：アクセス先が要求を受け付けませんでした。'
    if isinstance(e, requests.exceptions.ConnectionError):
        return '接続に失敗しました。通信経路やサイト側の制限が考えられます。'
    return f'通信エラー：{type(e).__name__}'


with st.sidebar:
    st.header('検索設定')
    category_url = st.text_input('ヨドバシカテゴリーURL', DEFAULT_URL)
    try:
        configured_key = os.getenv('KEEPA_API_KEY', '') or st.secrets.get('KEEPA_API_KEY', '')
    except Exception:
        configured_key = os.getenv('KEEPA_API_KEY', '')
    api_key = configured_key or st.text_input('Keepa APIキー', type='password')
    if configured_key:
        st.success('Keepa APIキー設定済み')
    threshold = 30
    st.info('抽出条件：Amazon価格より30%以上安い / 最低利益指定なし / ポイント考慮なし')
    max_items = st.number_input('確認商品数（試験運用）', min_value=1, max_value=30, value=10)
    st.caption('販売手数料・送料・ポイントは利益計算に含めません。')
    run = st.button('リサーチ開始', type='primary')

if run:
    if not valid_category(category_url):
        st.error('ヨドバシの正しいカテゴリーURLを入力してください。')
        st.stop()
    if not api_key:
        st.error('Keepa APIキーを設定してください。')
        st.stop()

    session = make_session()
    with st.status('ヨドバシへの接続を確認しています…', expanded=True) as status:
        try:
            response = session.get(category_url, timeout=(10, 25))
            response.raise_for_status()
            st.write(f'HTTPステータス：{response.status_code}')
            items = parse_yodobashi(response.text, category_url)[:int(max_items)]
            st.write(f'商品候補：{len(items)}件')
            status.update(label='カテゴリー取得処理が完了しました', state='complete')
        except requests.RequestException as e:
            status.update(label='カテゴリーの取得に失敗しました', state='error')
            st.error(friendly_error(e))
            st.info('再試行しても同じ場合、Streamlitのサーバーからヨドバシへのアクセスが制限されている可能性があります。アプリの更新だけでは解消できません。')
            st.stop()
    if not items:
        st.warning('ページには接続できましたが商品を抽出できませんでした。ページ構造やアクセス制限を確認してください。')
        st.stop()

    results, failures = [], []
    progress = st.progress(0)
    for i, item in enumerate(items):
        try:
            detail = session.get(item['ヨドバシURL'], timeout=(10, 20))
            detail.raise_for_status()
            jan = extract_jan(detail.text)
            if not jan:
                failures.append((item['商品名'], 'JAN未取得'))
                continue
            amazon = keepa_lookup(session, api_key, jan)
            if not amazon:
                failures.append((item['商品名'], 'Keepa商品またはAmazon価格未取得'))
                continue
            price = amazon['Amazon価格']
            difference = (price - item['ヨドバシ価格']) / price * 100
            if difference >= threshold:
                results.append({**item, 'JAN': jan, **amazon,
                                '価格差(%)': round(difference, 1),
                                '価格差(円)': price - item['ヨドバシ価格'],
                                'AmazonURL': f'https://www.amazon.co.jp/dp/{amazon["ASIN"]}'})
        except (requests.RequestException, ValueError, KeyError) as e:
            failures.append((item['商品名'], friendly_error(e) if isinstance(e, requests.RequestException) else str(e)[:120]))
        finally:
            progress.progress((i + 1) / len(items))
            time.sleep(0.3)

    st.metric('条件一致商品', len(results))
    st.caption(f'確認対象：{len(items)}件 / 照合不可：{len(failures)}件。価格は取得時点の参考値です。')
    if results:
        df = pd.DataFrame(results).sort_values('価格差(%)', ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={'ヨドバシURL': st.column_config.LinkColumn(),
                                    'AmazonURL': st.column_config.LinkColumn()})
        st.download_button('CSVを保存', df.to_csv(index=False).encode('utf-8-sig'),
                           'price_results.csv', 'text/csv')
        st.subheader('スマホ向け商品カード')
        for _, r in df.iterrows():
            with st.container(border=True):
                st.write('**' + str(r['商品名']) + '**')
                st.write(f"ヨドバシ ¥{r['ヨドバシ価格']:,} → Amazon ¥{r['Amazon価格']:,}")
                st.success(f"価格差 {r['価格差(%)']}% / ¥{r['価格差(円)']:,}")
                st.markdown(f"[ヨドバシで確認]({r['ヨドバシURL']})　|　[Amazonで確認]({r['AmazonURL']})")
    else:
        st.info('今回の取得範囲では条件に合う商品を確認できませんでした。')
    if failures:
        with st.expander(f'照合できなかった商品（{len(failures)}件）'):
            st.dataframe(pd.DataFrame(failures, columns=['商品名', '理由']), hide_index=True)
