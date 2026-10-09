import re
import os
import time
from urllib.parse import urljoin
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title='ヨドバシ × Keepa 価格差リサーチ', layout='wide', page_icon='🔎')
st.title('ヨドバシ × Amazon 価格差リサーチ')
st.caption('試作版：取得可能な公開ページとKeepa APIを使用。サイト側の制限やページ構造変更により取得できない場合があります。')
URL = 'https://www.yodobashi.com/category/141001/141336/'


def parse_yodobashi(html, base_url):
    soup = BeautifulSoup(html, 'lxml')
    found = {}
    for a in soup.select('a[href*="/product/"]'):
        href = urljoin(base_url, a.get('href', ''))
        if not re.search(r'/product/\d+', href):
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


def keepa_lookup(api_key, jan):
    # Keepa product lookup supports product codes (EAN/UPC) through the code parameter.
    response = requests.get('https://api.keepa.com/product', params={
        'key': api_key, 'domain': 5, 'code': jan, 'stats': 1
    }, timeout=30)
    response.raise_for_status()
    payload = response.json()
    products = payload.get('products') or []
    if not products:
        return None
    p = products[0]
    stats = p.get('stats') or {}
    current = stats.get('current') or []
    # Keepa indices: AMAZON=0, NEW=1, NEW_FBM_SHIPPING=7, BUY_BOX_SHIPPING=18.
    # Prefer Buy Box including shipping; fall back to new marketplace price.
    for idx in (18, 1, 0):
        if len(current) > idx and isinstance(current[idx], (int, float)) and current[idx] > 0:
            return {'ASIN': p.get('asin', ''), 'Amazon価格': current[idx], '価格指標': {18:'Buy Box（送料込み）',1:'新品出品価格',0:'Amazon本体価格'}[idx]}
    return None


def extract_jan(html):
    patterns = [r'(?:JANコード|JAN|EAN|GTIN)[^0-9]{0,50}(\d{13})', r'"gtin13"\s*:\s*"(\d{13})"']
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1)
    return None

with st.sidebar:
    st.header('検索設定')
    category_url = st.text_input('ヨドバシカテゴリーURL', URL)
    configured_key = os.getenv('KEEPA_API_KEY', '') or st.secrets.get('KEEPA_API_KEY', '')
    api_key = configured_key or st.text_input('Keepa APIキー', type='password', help='APIキーは第三者に共有しないでください。')
    if configured_key:
        st.success('Keepa APIキー設定済み')
    threshold = 30
    st.info('抽出条件：Amazon価格より30%以上安い / 最低利益指定なし / ポイント考慮なし')
    max_items = st.number_input('確認商品数（試験運用）', min_value=1, max_value=30, value=10)
    st.caption('価格差のみで抽出します。販売手数料や送料は抽出条件に含めません。')
    run = st.button('リサーチ開始', type='primary')

if run:
    if not category_url.startswith('https://www.yodobashi.com/category/'):
        st.error('ヨドバシのカテゴリーURLを入力してください。')
        st.stop()
    if not api_key:
        st.error('Keepa APIキーが必要です。')
        st.stop()
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; PriceResearchPrototype/1.0)'})
    try:
        response = session.get(category_url, timeout=25)
        response.raise_for_status()
        items = parse_yodobashi(response.text, category_url)[:max_items]
    except requests.RequestException as e:
        st.error(f'カテゴリーを取得できませんでした: {e}')
        st.stop()
    if not items:
        st.warning('商品情報を抽出できませんでした。ヨドバシのページ構造やアクセス制限が原因の可能性があります。')
        st.stop()
    results, failures = [], []
    bar = st.progress(0)
    for i, item in enumerate(items):
        try:
            detail = session.get(item['ヨドバシURL'], timeout=20)
            detail.raise_for_status()
            jan = extract_jan(detail.text)
            if not jan:
                failures.append((item['商品名'], 'JAN未取得'))
                continue
            amazon = keepa_lookup(api_key, jan)
            if not amazon:
                failures.append((item['商品名'], 'Amazon価格未取得'))
                continue
            price = amazon['Amazon価格']
            difference = (price - item['ヨドバシ価格']) / price * 100
            if difference >= threshold:
                results.append({**item, 'JAN': jan, **amazon, '価格差(%)': round(difference, 1), '価格差(円)': round(price - item['ヨドバシ価格']), 'AmazonURL': f'https://www.amazon.co.jp/dp/{amazon["ASIN"]}'})
        except (requests.RequestException, ValueError, KeyError) as e:
            failures.append((item['商品名'], str(e)[:100]))
        finally:
            bar.progress((i + 1) / len(items))
            time.sleep(0.3)
    st.metric('条件一致商品', len(results))
    st.caption(f'確認対象：{len(items)}件 / 照合不可：{len(failures)}件。価格はKeepa取得時点の参考値です。')
    if results:
        df = pd.DataFrame(results).sort_values('価格差(%)', ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True, column_config={'ヨドバシURL': st.column_config.LinkColumn(), 'AmazonURL': st.column_config.LinkColumn()})
        st.download_button('CSVを保存', df.to_csv(index=False).encode('utf-8-sig'), 'price_results.csv', 'text/csv')
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
            st.dataframe(pd.DataFrame(failures, columns=['商品名','理由']), hide_index=True)
