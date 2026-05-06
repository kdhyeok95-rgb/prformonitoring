import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import time
import urllib3
import urllib.parse
import re
import os
import tempfile
import importlib.util
import plotly.express as px
import google.generativeai as genai

# === 1. 기본 환경 설정 ===
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
st.set_page_config(page_title="통합 언론사 모니터링 시스템", layout="wide")

# === 2. 네이버 모니터링용 백엔드 스크립트 로드 ===
SCRIPT_FULL = os.path.join(os.path.dirname(__file__), "naver_news_to_csv.py")

def load_script(path, name):
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

full_news = load_script(SCRIPT_FULL, "full_news")
sentiment_colors = {"긍정": "blue", "부정": "red", "중립": "gray"}

# === 3. 공통 헬퍼 함수 ===
def load_and_clean_csv(file_or_path):
    try:
        df = pd.read_csv(file_or_path, encoding='utf-8-sig')
    except UnicodeDecodeError:
        if hasattr(file_or_path, 'seek'):
            file_or_path.seek(0)
        df = pd.read_csv(file_or_path, encoding='cp949')
    df.columns = df.columns.str.strip()
    return df

# 제목 기반 간단 우호도 분석 (시각화용)
def get_simple_sentiment(title):
    pos_words = ["최고", "호재", "상승", "강세", "신기록", "수상", "인정", "증가", "개선", "흑자", "성공", "선정", "수주", "협약", "확보", "혁신"]
    neg_words = ["하락", "약세", "적자", "벌금", "제재", "고발", "논란", "비리", "부정", "부실", "파산", "리콜", "부진", "사고", "화재", "파업", "갈등", "우려"]
    for w in neg_words:
        if w in title: return "부정"
    for w in pos_words:
        if w in title: return "긍정"
    return "중립"

# === AI 텍스트 요약 함수 (Gemini 2.5 Flash / 3.1 Pro 적용) ===
def summarize_with_gemini(text, api_key):
    if pd.isna(text) or not str(text).strip(): return "본문 데이터 없음"
    text_str = str(text)
    if not api_key or not api_key.strip(): 
        return text_str.strip()[:200] + "..." if len(text_str) > 200 else text_str.strip()
    try:
        genai.configure(api_key=api_key.strip())
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = f"다음 기사를 3문장 이내의 명확한 비즈니스 요약본으로 작성하십시오:\n\n{text_str[:3000]}"
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        error_msg = str(e)
        if "API_KEY_INVALID" in error_msg: return "[오류] 유효하지 않은 API 키입니다."
        elif "not found" in error_msg or "404" in error_msg: return "[오류] AI 모델 인식 실패."
        else: return f"[오류] 일시적 문제 발생: {error_msg}"

# === AI 종합 이슈 분석 리포트 ===
def generate_issue_report(titles, keyword, api_key):
    try:
        genai.configure(api_key=api_key.strip())
        model = genai.GenerativeModel('gemini-2.5-flash')
        title_list = "\n".join(titles)
        prompt = f"""
        당신은 홍보팀의 수석 데이터 분석가입니다. 다음은 '{keyword}'와 관련된 최근 언론 기사 제목들입니다.
        이 제목들을 분석하여 다음 양식에 맞게 마크다운으로 종합 보고서를 작성해 주세요.
        
        1. 🌟 주요 동향 (전체적인 흐름 요약)
        2. 📈 긍정적 요인 (호재나 긍정적 평가)
        3. ⚠️ 리스크 및 부정적 이슈 (당사 관련 부정적 언급, 논란, 사고 등. 없다면 '특이사항 없음'으로 기재)
        
        기사 제목 목록:
        {title_list}
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"리포트 생성 중 오류가 발생했습니다: {e}"

# === 4. 지역 언론사 크롤링 로직 ===
NDSOFT_GROUP = ["중부매일", "충청신문", "충남팩트뉴스", "중앙매일", "투데이충남", "충남뉴스통신", "메가충청뉴스", "로컬투데이", "대전투데이", "충남인터넷뉴스"]

def get_article_links(soup, media_name, base_url):
    valid_links = []
    seen = set()
    clean_base = str(base_url).rstrip('/')
    for a in soup.find_all('a', href=True):
        href = a['href']
        title = a.get_text(separator=' ', strip=True)
        if not title or len(title) < 3: continue
            
        is_match = False
        if media_name in ["시대일보", "e당진뉴스", "충남신문"]:
            if re.search(r'^\/\d{4,8}$', href): is_match = True
        elif media_name == "충청탑뉴스":
            if "aid=" in href or ("class" in a.attrs and "sublist" in a.get("class", [])): is_match = True
        elif media_name == "충남뉴스통신":
            if "idxno=" in href or ("class" in a.attrs and "links" in a.get("class", [])): is_match = True
        elif media_name == "당진투데이":
            if a.find(class_='title') or re.search(r'(idxno=|no=|seq=|idx=)', href, re.IGNORECASE): is_match = True
        else:
            if "idxno=" in href: is_match = True
                
        if is_match:
            abs_link = href if href.startswith('http') else f"{clean_base}/{href.lstrip('/')}"
            if abs_link not in seen:
                seen.add(abs_link)
                valid_links.append({"title": title, "link": abs_link})
    return valid_links

def extract_article_date(soup):
    header_area = soup.select_one('.info_line, .article-head, .view-info, .news_info, .date_wrap, .date, .list_date, .byline, .info-text')
    if header_area:
        match = re.search(r'(?:승인|입력|등록|작성일|기사출고)\s*[:\s]*\w*\s*(20[12]\d)\s*[-./년]\s*(0?[1-9]|1[0-2])\s*[-./월]\s*(0?[1-9]|[12]\d|3[01])', header_area.text)
        if match:
            y, m, d = map(int, match.groups())
            try: return datetime(y, m, d).date(), f"{y}-{m:02d}-{d:02d}"
            except ValueError: pass
            
        match = re.search(r'(20[12]\d)\s*[-./년]\s*(0?[1-9]|1[0-2])\s*[-./월]\s*(0?[1-9]|[12]\d|3[01])', header_area.text)
        if match:
            y, m, d = map(int, match.groups())
            try: return datetime(y, m, d).date(), f"{y}-{m:02d}-{d:02d}"
            except ValueError: pass

    meta_date = soup.find('meta', property='article:published_time') or soup.find('meta', attrs={'name': 'article:published_time'})
    if meta_date and meta_date.get('content'):
        match = re.search(r'(20[12]\d)[-./](0?[1-9]|1[0-2])[-./](0?[1-9]|[12]\d|3[01])', meta_date['content'])
        if match:
            y, m, d = map(int, match.groups())
            return datetime(y, m, d).date(), f"{y}-{m:02d}-{d:02d}"

    return None, "날짜 알수없음"

def scrape_local_news(media_name, base_url, csv_search_url, keyword, start_date, end_date, gemini_api_key):
    logs, results = [], []
    clean_base = str(base_url).rstrip('/')
    utf8_k = urllib.parse.quote(keyword.encode('utf-8'))
    euckr_k = urllib.parse.quote(keyword.encode('euc-kr'))
    
    if media_name in NDSOFT_GROUP or pd.isna(csv_search_url) or not str(csv_search_url).strip():
        search_url = f"{clean_base}/news/articleList.html?sc_area=A&view_type=sm&sc_word={utf8_k}"
    else:
        search_url = str(csv_search_url).replace("현대제철", keyword).replace("%ED%98%84%EB%8C%80%EC%A0%9C%EC%B2%A0", utf8_k).replace("%C7%F6%B4%EB%C1%A6%C3%B6", euckr_k)

    headers = {'User-Agent': 'Mozilla/5.0'} 
    
    try:
        response = requests.get(search_url, headers=headers, timeout=10, verify=False)
        soup = BeautifulSoup(response.content, 'html.parser')
        article_links = get_article_links(soup, media_name, base_url)
        
        if not article_links: return results, logs

        for item in article_links:
            title, link = item['title'], item['link']
            try:
                art_res = requests.get(link, headers=headers, timeout=5, verify=False)
                art_soup = BeautifulSoup(art_res.content, 'html.parser')
                content_tag = art_soup.select_one("#article-view-content-div, .article-body, #articleBody, #news_body_area, .txt_box, .view_cont")
                content_text = content_tag.text.strip() if content_tag else art_soup.text.strip()
                
                if keyword not in title and keyword not in content_text: continue
                
                dt_obj, date_str = extract_article_date(art_soup)
                if dt_obj:
                    if not (start_date <= dt_obj <= end_date): continue 
                else: continue

                summary = summarize_with_gemini(content_text, gemini_api_key)
                sentiment = get_simple_sentiment(title)

                results.append({
                    "언론사": media_name,
                    "게시일자": date_str,
                    "제목": title.replace('\n', ' ').strip(),
                    "우호도": sentiment,
                    "요약내용": summary.replace('\n', ' ').strip(),
                    "링크": link
                })
                time.sleep(0.2)
            except Exception:
                continue
        logs.append(f"[SUCCESS] {media_name} - {len(results)}건 수집 완료")
    except Exception as e:
        logs.append(f"[ERROR] {media_name} - {e}")
    return results, logs 


# ==========================================
# === 5. 통합된 UI 레이아웃 (공통 사이드바) ===
# ==========================================

st.sidebar.title("⚙️ 시스템 통합 설정")
system_mode = st.sidebar.radio("모니터링 대상 전환", ["비제휴 지역 언론사", "네이버 포털 뉴스"], index=0)
st.sidebar.markdown("---")

# ✨ 공통 적용되는 핵심 변수들
st.sidebar.subheader("검색 및 AI 설정")
gemini_api_key = st.sidebar.text_input("🔑 Gemini API Key (선택)", type="password", help="강력한 Gemini 요약 및 리스크 분석을 위해 입력하세요.")
keyword = st.sidebar.text_input("분석 대상 키워드", value="현대제철")
col1, col2 = st.sidebar.columns(2)
start_date = col1.date_input("수집 시작일")
end_date = col2.date_input("수집 종료일")

# 💡 [핵심 해결] 세션 상태 초기화 (데이터 증발 방지)
if 'local_news_df' not in st.session_state:
    st.session_state['local_news_df'] = None
if 'naver_news_df' not in st.session_state:
    st.session_state['naver_news_df'] = None
if 'naver_output_csv' not in st.session_state:
    st.session_state['naver_output_csv'] = None

# ------------------------------------------
# [탭 1] 비제휴 지역 언론사 모니터링
# ------------------------------------------
if system_mode == "비제휴 지역 언론사":
    st.title("지역 언론사 대상 뉴스 모니터링 및 시각화")
    
    st.sidebar.markdown("---")
    st.sidebar.subheader("지역 언론사 전용 설정")
    default_csv_path = "언론사 홈페이지.csv"
    df_media = None

    if os.path.exists(default_csv_path):
        df_media = load_and_clean_csv(default_csv_path)
        st.sidebar.success(f"데이터베이스 연동 완료")
    else:
        st.sidebar.warning(f"'{default_csv_path}' 파일이 없습니다.")
        uploaded_file = st.sidebar.file_uploader("언론사 목록 업로드", type=['csv'])
        if uploaded_file: df_media = load_and_clean_csv(uploaded_file)

    if st.sidebar.button("지역 언론사 수집 실행", type="primary"):
        if df_media is None or '구분' not in df_media.columns or '홈페이지 주소' not in df_media.columns:
            st.error("데이터 구조 오류: CSV 파일에 '구분'과 '홈페이지 주소' 열이 필요합니다.")
        else:
            with st.spinner("지역 언론사를 파싱하고 데이터를 필터링 중입니다..."):
                all_news_data = []
                total_media = len(df_media)
                progress_bar = st.progress(0, text="초기화 중...")
                
                for index, row in df_media.iterrows():
                    progress_bar.progress((index + 1) / total_media, text=f"수집 중: {row['구분']}")
                    csv_search_url = row['검색주소창'] if '검색주소창' in df_media.columns else None
                    news_data, _ = scrape_local_news(row['구분'], row['홈페이지 주소'], csv_search_url, keyword, start_date, end_date, gemini_api_key)
                    all_news_data.extend(news_data)
                progress_bar.empty() 
            
            if all_news_data:
                result_df = pd.DataFrame(all_news_data).sort_values(by='게시일자', ascending=True).reset_index(drop=True)
                st.session_state['local_news_df'] = result_df # 창고에 저장
                st.rerun()
            else:
                st.warning("조건에 맞는 기사가 없습니다.")
                st.session_state['local_news_df'] = None

    # 데이터가 창고에 있다면 표시
    if st.session_state['local_news_df'] is not None:
        result_df = st.session_state['local_news_df']
        st.success(f"총 {len(result_df)}건의 지역 기사가 수집되었습니다.")
        st.dataframe(result_df, use_container_width=True)
        
        csv = result_df.to_csv(index=False, encoding='utf-8-sig')
        st.download_button("결과 리포트 다운로드 (CSV)", data=csv.encode('utf-8-sig'), file_name=f"Local_News_{keyword}.csv", mime='text/csv')

        st.markdown("---")
        st.header("📊 지역 언론사 분석 시각화")
        
        col_chart1, col_chart2 = st.columns(2)
        with col_chart1:
            st.subheader("우호도 비율")
            fig_pie = px.pie(result_df, names="우호도", color="우호도", color_discrete_map=sentiment_colors, hole=0.3)
            st.plotly_chart(fig_pie, use_container_width=True)
        
        with col_chart2:
            st.subheader("언론사별 보도량")
            df_press = result_df["언론사"].value_counts().reset_index()
            df_press.columns = ["언론사", "기사수"]
            fig_bar = px.bar(df_press, x="언론사", y="기사수", text="기사수", color="언론사")
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("날짜별 보도량 추이")
        df_trend = result_df.groupby("게시일자").size().reset_index(name="기사수")
        fig_line = px.line(df_trend, x="게시일자", y="기사수", markers=True)
        st.plotly_chart(fig_line, use_container_width=True)

        if gemini_api_key:
            st.markdown("---")
            st.header("🧠 Gemini 종합 분석 리포트")
            if st.button("AI 리스크 및 동향 분석 실행", key="local_ai_btn"):
                with st.spinner("AI가 부정적 이슈와 종합 동향을 분석 중입니다..."):
                    titles = result_df['제목'].tolist()
                    report = generate_issue_report(titles, keyword, gemini_api_key)
                    st.info(report)
        else:
            st.info("💡 좌측 사이드바에 Gemini API Key를 입력하시면 AI 종합 분석 리포트(부정적 이슈 파악)를 생성할 수 있습니다.")


# ------------------------------------------
# [탭 2] 네이버 포털 뉴스 모니터링
# ------------------------------------------
elif system_mode == "네이버 포털 뉴스":
    st.title("📰 네이버 언론 보도 분석 대시보드")
    
    if full_news is None:
        st.error("⚠️ `naver_news_to_csv.py` 스크립트를 찾을 수 없습니다.")
    else:
        st.sidebar.markdown("---")
        st.sidebar.subheader("네이버 전용 설정")
        mode = st.sidebar.radio("분석 모드", ["간단 분석", "정밀 분석"], index=1)
        total = st.sidebar.slider("수집 기사 수 (최대 1000)", 10, 1000, 300, 10)
        whitelist_only = st.sidebar.checkbox("출입 언론사 기사만 포함 (정밀 전용)", value=False, disabled=(mode=="간단 분석"))

        if st.sidebar.button("네이버 뉴스 수집 실행", type="primary"):
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_csv = os.path.join(tempfile.gettempdir(), f"naver_news_{now_str}.csv")

            with st.spinner("네이버 포털 뉴스를 수집 중입니다..."):
                try:
                    full_news.run(
                        query=keyword, total=total, out_csv=output_csv,
                        whitelist_only=bool(whitelist_only), start_dt=start_date,
                        end_dt=end_date, excel=False, simple_mode=(mode=="간단 분석")
                    )

                    df = pd.read_csv(output_csv)
                    if "게시일" in df.columns: df["게시일"] = pd.to_datetime(df["게시일"], errors="coerce").dt.date
                    
                    st.session_state['naver_news_df'] = df # 창고에 저장
                    st.session_state['naver_output_csv'] = output_csv
                    st.rerun()

                except Exception as e:
                    st.error(f"❌ 수집 중 오류 발생: {e}")

        # 데이터가 창고에 있다면 표시
        if st.session_state['naver_news_df'] is not None:
            df = st.session_state['naver_news_df']
            output_csv = st.session_state['naver_output_csv']
            
            st.success(f"총 {len(df)}건의 네이버 기사가 수집되었습니다.")

            # 💡 [핵심 해결] 네이버 뉴스 요약 누락 채우기
            if gemini_api_key and ("요약" in df.columns) and ("요약내용" not in df.columns):
                with st.spinner("AI가 네이버 기사의 핵심 내용을 정밀 요약하고 있습니다..."):
                    df["요약내용"] = df["요약"].apply(lambda x: summarize_with_gemini(str(x), gemini_api_key))
                    st.session_state['naver_news_df'] = df # 요약 추가 후 창고 업데이트
            
            st.dataframe(df, use_container_width=True, height=400)

            # 다운로드
            if output_csv and os.path.exists(output_csv):
                with open(output_csv, "rb") as f:
                    st.download_button("네이버 결과 CSV 다운로드", data=f, file_name=f"Naver_News_{keyword}.csv", mime="text/csv")

            st.markdown("---")
            st.header("📊 네이버 뉴스 분석 시각화")
            col1, col2 = st.columns(2)

            if "우호도" in df.columns and not df["우호도"].empty:
                with col1:
                    st.subheader("우호도 비율")
                    fig = px.pie(df, names="우호도", color="우호도", color_discrete_map=sentiment_colors, hole=0.3)
                    st.plotly_chart(fig, use_container_width=True)

            if "게시일" in df.columns:
                with col2:
                    st.subheader("날짜별 기사량 추이")
                    df_count = df.groupby("게시일").size().reset_index(name="기사수")
                    fig3 = px.line(df_count, x="게시일", y="기사수", markers=True)
                    st.plotly_chart(fig3, use_container_width=True)

            if mode == "정밀 분석" and "매체" in df.columns and not df["매체"].isna().all():
                st.subheader("보도량 상위 매체")
                df_press = df["매체"].value_counts().head(10).reset_index()
                df_press.columns = ["매체", "기사수"]
                fig4 = px.bar(df_press, x="매체", y="기사수", color="매체")
                st.plotly_chart(fig4, use_container_width=True)

            # ✨ Gemini 이슈 리포트 ✨
            if gemini_api_key:
                st.markdown("---")
                st.header("🧠 Gemini 종합 분석 리포트")
                if st.button("AI 리스크 및 동향 분석 실행", key="naver_ai_btn"):
                    with st.spinner("AI가 부정적 이슈와 종합 동향을 분석 중입니다..."):
                        titles = df['제목'].tolist()
                        report = generate_issue_report(titles, keyword, gemini_api_key)
                        st.info(report)
            else:
                st.info("💡 좌측 사이드바에 Gemini API Key를 입력하시면 AI 종합 분석 리포트(부정적 이슈 파악)를 생성할 수 있습니다.")