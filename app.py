import os
import re
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="ヨドバシ × Keepa 価格差リサーチ", page_icon="🔎", layout="wide")
st.title("ヨドバシ × Amazon 価格差リサーチ")
st.caption("診断付き試作版。公開ページへのアクセス制限がある場合、取得はできません。")
DEFAULT_URL = "https://www.yodobashi.com/category/141001/141336/"


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PriceResearch/1.0)", "Accept": "text/html,application/xhtml+xml"})
    return s


def valid_url(url):
    p = urlparse(url)
    return p.scheme == "https" and p.hostname in ("www.yodobashi.com", "yodobashi.com") and p.path.startswith("/category/")


def parse_items(html, base):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.select('a[href*="/product/"]'):
        url = urljoin(base, a.get("href", ""))
        if urlparse(url).hostname not in ("www.yodobashi.com", "yodobashi.com") or not re.search(r"/product/\d+", url):
            continue
        block = a.find_parent(["li", "article"]) or a.parent
        txt = block.get_text(" ", strip=True)
        m = re.search(r"[￥¥]\s*([\d,]+)", txt)
        name = a.get_text(" ", strip=True)
        if m and name:
            price = int(m.group(1).replace(",", ""))
            if price > 0:
                found[url] = {"商品名": name[:160], "ヨドバシ価格": price, "ヨドバシURL": url}
    return list(found.values())


def get_jan(html):
    for pattern in (r'(?:JANコード|JAN|EAN|GTIN)[^0-9]{0,50}(\d{13})', r'"gtin13"\s*:\s*"(\d{13})"'):
        m = re.search(pattern, html, re.I)
        if m:
            return m.group(1)
    return None


def keepa_price(s, key, jan):
    r = s.get("https://api.keepa.com/product", params={"key": key, "domain": 5, "code": jan, "stats": 1}, timeout=(10, 30))
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise ValueError(f"Keepa APIエラー: {str(data['error'])[:120]}")
    products = data.get("products") or []
    if not products:
        return None
    product = products[0]
    current = (product.get("stats") or {}).get("current") or []
    for idx, label in ((18, "カート価格（送料込み）"), (1, "新品出品価格"), (0, "Amazon本体価格")):
        if len(current) > idx and isinstance(current[idx], (int, float)) and current[idx] > 0:
            return {"ASIN": product.get("asin", ""), "Amazon価格": int(current[idx]), "価格指標": label}
    return None


def error_text(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} ({e.response.url.split('?')[0]})"
    return f"{type(e).__name__}: {str(e)[:160]}"


with st.sidebar:
    st.header("検索設定")
    category = st.text_input("ヨドバシカテゴリーURL", DEFAULT_URL)
    try:
        configured = os.getenv("KEEPA_API_KEY", "") or st.secrets.get("KEEPA_API_KEY", "")
    except Exception:
        configured = os.getenv("KEEPA_API_KEY", "")
    key = configured or st.text_input("Keepa APIキー", type="password")
    if configured:
        st.success("Keepa APIキー設定済み（通信は未確認）")
    max_items = st.number_input("確認商品数", min_value=1, max_value=30, value=10)
    st.info("抽出条件：Amazon価格より30%以上安い。手数料・送料・ポイントは利益計算に含めません。")
    run = st.button("リサーチ開始", type="primary")

if run:
    if not valid_url(category):
        st.error("正しいヨドバシのカテゴリーURLを入力してください。")
        st.stop()
    if not key:
        st.error("Keepa APIキーが未設定です。")
        st.stop()
    s = session()
    st.subheader("接続診断")
    try:
        r = s.get(category, timeout=(10, 25))
        st.write(f"ヨドバシ応答：HTTP {r.status_code} / 取得文字数：{len(r.text):,}")
        r.raise_for_status()
    except requests.RequestException as e:
        st.error("カテゴリー取得に失敗：" + error_text(e))
        st.info("サーバー側のアクセス制限の場合、HTML解析コードを変更しても解決しません。")
        st.stop()
    items = parse_items(r.text, category)
    st.write(f"抽出できた商品候補：{len(items)}件")
    if not items:
        st.warning("接続は成功しましたが商品が見つかりません。ページ構造または配信内容の確認が必要です。")
        st.stop()
    items = items[:int(max_items)]
    results, failures = [], []
    progress = st.progress(0)
    for i, item in enumerate(items):
        try:
            detail = s.get(item["ヨドバシURL"], timeout=(10, 20))
            detail.raise_for_status()
            jan = get_jan(detail.text)
            if not jan:
                failures.append({"商品名": item["商品名"], "段階": "JAN抽出", "理由": "JANが見つかりません"})
                continue
            amazon = keepa_price(s, key, jan)
            if not amazon:
                failures.append({"商品名": item["商品名"], "段階": "Keepa照合", "理由": "商品または価格データなし"})
                continue
            difference = (amazon["Amazon価格"] - item["ヨドバシ価格"]) / amazon["Amazon価格"] * 100
            if difference >= 30:
                results.append({**item, "JAN": jan, **amazon, "価格差(%)": round(difference, 1), "価格差(円)": amazon["Amazon価格"] - item["ヨドバシ価格"], "AmazonURL": f'https://www.amazon.co.jp/dp/{amazon["ASIN"]}'})
        except (requests.RequestException, ValueError, KeyError) as e:
            failures.append({"商品名": item["商品名"], "段階": "商品取得またはKeepa照合", "理由": error_text(e)})
        finally:
            progress.progress((i + 1) / len(items))
    st.metric("価格差30%以上の商品", len(results))
    st.caption(f"確認商品：{len(items)}件 / 照合失敗：{len(failures)}件")
    if results:
        df = pd.DataFrame(results).sort_values("価格差(%)", ascending=False)
        st.dataframe(df, hide_index=True, use_container_width=True, column_config={"ヨドバシURL": st.column_config.LinkColumn(), "AmazonURL": st.column_config.LinkColumn()})
        st.download_button("CSV保存", df.to_csv(index=False).encode("utf-8-sig"), "price_results.csv", "text/csv")
    else:
        st.info("今回の範囲では条件に一致する商品は確認できませんでした。")
    if failures:
        with st.expander(f"診断詳細：照合できなかった商品（{len(failures)}件）", expanded=True):
            st.dataframe(pd.DataFrame(failures), hide_index=True, use_container_width=True)
