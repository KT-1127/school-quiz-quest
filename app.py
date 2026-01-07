import streamlit as st
import fitz  # PyMuPDF
import requests
import json
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import datetime
from PIL import Image
import io
import base64
from collections import Counter
import random
import pandas as pd

# =========================================================
# 1. APIキー & 設定
# =========================================================
if "GOOGLE_API_KEY" in st.secrets:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
else:
    st.error("🚨 APIキーが設定されていません。")
    st.stop()

if not firebase_admin._apps:
    try:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    except:
        if "firebase" in st.secrets:
            key_dict = json.loads(st.secrets["firebase"]["json_key"])
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred)

db = firestore.client()

st.set_page_config(page_title="スクールクイズ Quest", layout="wide", page_icon="🏫")

st.markdown("""
<style>
    .stApp { background: linear-gradient(to bottom right, #fdfbfb, #ebedee); }
    .quiz-card { background: white; padding: 20px; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); margin-bottom: 20px; border-left: 6px solid #4CAF50; }
    .big-font { font-size: 20px !important; font-weight: bold; }
    div[data-testid="stMetricValue"] { font-size: 1.5rem; }
</style>
""", unsafe_allow_html=True)

CATEGORIES = ["興福寺国宝館", "東大寺大仏殿", "奈良公園", "大江能楽堂", "SDGs関係"]

# クイズ用・ランキング用 共通
RANKING_CATEGORIES = ["ランダム10選", "👍 いいねベスト10"] + CATEGORIES


# =========================================================
# 2. 画像・AI解析関数
# =========================================================
def get_background_xrefs(doc):
    xref_counts = Counter()
    for page in doc:
        for img in page.get_images(full=True):
            xref_counts[img[0]] += 1
    if len(doc) > 1:
        return {xref for xref, count in xref_counts.items() if count > 1}
    return set()

def compress_image(pil_img):
    if pil_img.mode != "RGB": pil_img = pil_img.convert("RGB")
    pil_img.thumbnail((600, 600))
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()

def get_unique_image(doc, page, background_xrefs):
    image_list = page.get_images(full=True)
    if not image_list: return None
    candidates = []
    for img in image_list:
        xref = img[0]
        if xref in background_xrefs: continue
        try:
            base_image = doc.extract_image(xref)
            pil_img = Image.open(io.BytesIO(base_image["image"]))
            w, h = pil_img.size
            if w < 50 or h < 50: continue
            if w / h > 6 or w / h < 0.15: continue
            candidates.append({"img": pil_img, "area": w * h})
        except: continue
    if not candidates: return None
    candidates.sort(key=lambda x: x["area"], reverse=True)
    return compress_image(candidates[0]["img"])

def analyze_pdf(uploaded_file, show_name, user_nickname):
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    background_xrefs = get_background_xrefs(doc)
    quizzes = []
    
    api_url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash-lite:generateContent?key={GOOGLE_API_KEY}"
    headers = {'Content-Type': 'application/json'}

    # シンプルなスピナー表示のみに変更
    with st.spinner("⏳ 解析中..."):
        for i, page in enumerate(doc):
            text = page.get_text()
            
            # サンプル除外ロジック
            if "阿修羅像" in text and ("感情" in text or "顔" in text): continue
            if any(k in text for k in ["クイズの例", "例題", "練習問題", "サンプル"]): continue

            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            page_img_b64 = base64.b64encode(io.BytesIO(pix.tobytes("png")).getvalue()).decode()
            unique_img_b64 = get_unique_image(doc, page, background_xrefs)

            prompt = f"""
            あなたは教師です。画像からクイズを作成してください。
            
            【重要ルール】
            1. 「クイズの例」「例題」などのページは除外し、空リスト [] を返してください。
            2. 問題文の内容を読み、以下のリストから最も適切なカテゴリを1つ選んでください。
               リスト: {CATEGORIES}
            3. 出力にはJSONデータ以外は一切含めないでください。
            
            【出力JSON】
            [
                {{ 
                    "category": "カテゴリ名",
                    "question": "問題文", 
                    "choices": ["選択肢1", "選択肢2", "選択肢3", "選択肢4"], 
                    "answer": "正解と解説", 
                    "correct_index": 0,
                    "needs_image": true/false 
                }}
            ]
            """
            payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": page_img_b64}}]}]}
            
            try:
                res = requests.post(api_url, headers=headers, data=json.dumps(payload))
                if res.status_code != 200: continue
                
                raw_text = res.json()['candidates'][0]['content']['parts'][0]['text']
                start_idx = raw_text.find('[')
                end_idx = raw_text.rfind(']') + 1
                if start_idx == -1 or end_idx == 0: continue
                
                json_str = raw_text[start_idx:end_idx]
                data = json.loads(json_str)
                
                for q in data:
                    q_text = q.get("question", "")
                    if "阿修羅像" in q_text and ("感情" in q_text or "顔" in q_text): continue
                    if "クイズの例" in q_text: continue

                    img_list = []
                    if q.get("needs_image") and unique_img_b64:
                        img_list.append(unique_img_b64)
                    
                    choices = q.get("choices", [])
                    if isinstance(choices, str): choices = choices.split("\n")

                    cat = q.get("category", "")
                    if cat not in CATEGORIES: cat = "その他"

                    creator_name = user_nickname if show_name else "匿名"

                    quizzes.append({
                        "category": cat,
                        "question": q_text,
                        "choices": choices,
                        "correct_index": q.get("correct_index", 0),
                        "answer": str(q["answer"]),
                        "images": img_list,
                        "created_by": creator_name,
                        "created_at": datetime.datetime.now(),
                        "likes": 0
                    })
            except: continue

    return quizzes

# =========================================================
# 3. ログイン画面
# =========================================================
def login_page():
    st.title("🏫 ログイン")
    col1, col2 = st.columns([1, 2])
    
    users_ref = db.collection("users")
    docs = users_ref.stream()
    user_dict = {doc.to_dict()["real_name"]: doc for doc in docs}
    user_names = sorted(list(user_dict.keys()))
    
    with col1:
        if not user_names:
            st.warning("ユーザーがいません。")
            with st.expander("管理者作成"):
                a_name = st.text_input("管理者名")
                a_pass = st.text_input("パスワード", type="password")
                if st.button("作成"):
                    db.collection("users").add({
                        "real_name": a_name, "password": a_pass, "nickname": a_name, "role": "teacher",
                        "created_at": datetime.datetime.now(), "score": 0, "category_scores": {}
                    })
                    st.rerun()
        else:
            name = st.selectbox("名前を選択", ["選択してください"] + user_names)
            password = st.text_input("パスワード", type="password")
            
            if st.button("ログイン", type="primary"):
                if name != "選択してください":
                    user_doc = user_dict[name]
                    u_data = user_doc.to_dict()
                    if u_data["password"] == password:
                        u_data["uid"] = user_doc.id
                        st.session_state["user"] = u_data
                        st.success("ログイン成功！")
                        st.rerun()
                    else:
                        st.error("パスワードが違います")

# =========================================================
# 4. アプリ本体
# =========================================================
if "user" not in st.session_state:
    login_page()
    st.stop()

user = st.session_state["user"]

with st.sidebar:
    st.write(f"👤 **{user['nickname']}**")
    with st.expander("ニックネーム変更"):
        nn = st.text_input("新しい名前", value=user['nickname'])
        if st.button("変更"):
            db.collection("users").document(user["uid"]).update({"nickname": nn})
            st.session_state["user"]["nickname"] = nn
            st.rerun()
            
    if st.button("ログアウト"):
        del st.session_state["user"]
        st.rerun()
        
    st.divider()
    menu_ops = ["🎮 クイズを解く", "📝 問題を作る", "🏆 ランキング"]
    if user["role"] == "teacher": menu_ops.append("👨‍🏫 先生メニュー")
    menu = st.radio("メニュー", menu_ops)

# --- 先生メニュー ---
if menu == "👨‍🏫 先生メニュー":
    st.header("👨‍🏫 管理画面")
    tab1, tab2 = st.tabs(["一括登録", "成績"])
    with tab1:
        txt = st.text_area("名前,パスワード (1行に1人)", height=150)
        if st.button("登録"):
            batch = db.batch()
            for line in txt.strip().split("\n"):
                if "," in line:
                    n, p = line.split(",")
                    ref = db.collection("users").document()
                    batch.set(ref, {
                        "real_name": n.strip(), "password": p.strip(), "nickname": n.strip(), "role": "student",
                        "created_at": datetime.datetime.now(), "score": 0, "category_scores": {}
                    })
            batch.commit()
            st.success("登録しました")
    with tab2:
        if st.button("更新"): st.rerun()
        docs = db.collection("users").stream()
        data = []
        for d in docs:
            dd = d.to_dict()
            row = {"名前": dd.get("real_name"), "ニックネーム": dd.get("nickname")}
            cat_scores = dd.get("category_scores", {})
            for c in RANKING_CATEGORIES:
                row[c] = cat_scores.get(c, 0)
            data.append(row)
        st.dataframe(pd.DataFrame(data))

# --- 問題作成 ---
elif menu == "📝 問題を作る":
    st.header("📝 問題作成")
    st.info("PDFをアップロードしてください。")
    
    uploaded_file = st.file_uploader("PDFアップロード", type=["pdf"])
    
    if uploaded_file:
        st.write("▼ 設定を選んで解析を開始")
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("👤 名前を表示して投稿", type="primary", use_container_width=True):
                # 関数内で spinner を使用しているため、ここでは不要
                qs = analyze_pdf(uploaded_file, True, user["nickname"])
                if qs:
                    batch = db.batch()
                    cnt = 0
                    for q in qs:
                        ref = db.collection("quizzes").document()
                        batch.set(ref, q)
                        cnt += 1
                        if cnt >= 400:
                            batch.commit()
                            batch = db.batch()
                            cnt = 0
                    batch.commit()
                    st.success(f"{len(qs)}問 作成しました！")
                else:
                    st.error("クイズが見つかりませんでした")
        
        with col2:
            if st.button("🕶️ 匿名で投稿", use_container_width=True):
                qs = analyze_pdf(uploaded_file, False, user["nickname"])
                if qs:
                    batch = db.batch()
                    cnt = 0
                    for q in qs:
                        ref = db.collection("quizzes").document()
                        batch.set(ref, q)
                        cnt += 1
                        if cnt >= 400:
                            batch.commit()
                            batch = db.batch()
                            cnt = 0
                    batch.commit()
                    st.success(f"{len(qs)}問 作成しました！")
                else:
                    st.error("クイズが見つかりませんでした")

# --- クイズ ---
elif menu == "🎮 クイズを解く":
    st.header("🎮 クイズ")
    mode = st.selectbox("コース", RANKING_CATEGORIES)
    
    if st.button("スタート"):
        st.session_state["quiz_mode"] = True
        st.session_state["q_idx"] = 0
        st.session_state["session_score"] = 0
        st.session_state["answer_state"] = None
        st.session_state["current_mode"] = mode
        
        ref = db.collection("quizzes")
        
        if mode == "ランダム10選":
            docs = list(ref.limit(50).stream())
            if len(docs) > 0:
                st.session_state["q_list"] = random.sample(docs, min(len(docs), 10))
            else:
                st.session_state["q_list"] = []
                
        elif mode == "👍 いいねベスト10":
            docs = list(
                ref.order_by("likes", direction=firestore.Query.DESCENDING)
                .limit(10)
                .stream()
            )
            st.session_state["q_list"] = docs

        else:
            docs = list(ref.where("category", "==", mode).limit(50).stream())
            if len(docs) > 0:
                st.session_state["q_list"] = random.sample(docs, min(len(docs), 10))
            else:
                st.session_state["q_list"] = []
            
        st.rerun()

    if st.session_state.get("quiz_mode"):
        q_list = st.session_state["q_list"]
        idx = st.session_state["q_idx"]
        
        if not q_list:
            st.warning("問題がありません")
            if st.button("戻る"): del st.session_state["quiz_mode"]; st.rerun()
            st.stop()

        if idx < len(q_list):
            doc = q_list[idx]
            q = doc.to_dict()
            qid = doc.id
            
            realtime_doc = db.collection("quizzes").document(qid).get()
            current_likes = realtime_doc.to_dict().get("likes", 0)
            
            st.progress((idx+1)/len(q_list))
            
            # タイトル更新
            
            
            if q.get("images"):
                for img in q["images"]:
                    st.image(Image.open(io.BytesIO(base64.b64decode(img))), width=300)
            
            st.markdown(f"**{q['question']}**")
            
            cols = st.columns(2)
            choices = q["choices"]
            
            if st.session_state["answer_state"] is None:
                for i, c in enumerate(choices):
                    if cols[i%2].button(c, key=f"q{idx}c{i}", use_container_width=True):
                        st.session_state["answer_state"] = i
                        st.rerun()
            else:
                user_ans = st.session_state["answer_state"]
                correct = q.get("correct_index", 0)
                
                if user_ans == correct:
                    st.success("⭕ 正解！")
                    if "counted" not in st.session_state:
                        st.session_state["session_score"] += 1
                        st.session_state["counted"] = True
                else:
                    st.error(f"❌ 不正解... 正解は: {choices[correct]}")
                    st.info(f"解説: {q.get('answer')}")
                
                # いいね機能
                like_ref = db.collection("quizzes").document(qid).collection("likes").document(user["uid"])
                is_liked = like_ref.get().exists
                
                btn_label = "❤️ いいねを取り消す" if is_liked else "❤️ いいね！"
                
                if st.button(btn_label, key=f"like{idx}"):
                    if is_liked:
                        like_ref.delete()
                        db.collection("quizzes").document(qid).update({"likes": firestore.Increment(-1)})
                    else:
                        like_ref.set({"ts": datetime.datetime.now()})
                        db.collection("quizzes").document(qid).update({"likes": firestore.Increment(1)})
                    st.rerun()
                
                st.caption(f"現在のいいね: {current_likes}")

                if st.button("次の問題へ"):
                    st.session_state["q_idx"] += 1
                    st.session_state["answer_state"] = None
                    if "counted" in st.session_state: del st.session_state["counted"]
                    st.rerun()

        else:
            st.balloons()
            st.markdown(f"## 🎉 結果発表")
            score = st.session_state["session_score"]
            st.markdown(f"### {len(q_list)}問中 {score}問正解")
            
            if st.button("終了"):
                mode = st.session_state["current_mode"]
                uref = db.collection("users").document(user["uid"])
                
                # ハイスコア更新処理
                user_data = uref.get().to_dict()
                current_scores = user_data.get("category_scores", {})
                best_score = current_scores.get(mode, 0)
                
                # いいね順以外の場合に記録
                if mode != "👍 いいねベスト10":
                    if score > best_score:
                        uref.update({f"category_scores.{mode}": score})
                        st.toast(f"🎉 自己ベスト更新！ ({best_score}点 → {score}点)")
                
                del st.session_state["quiz_mode"]
                st.rerun()

# --- ランキング ---
elif menu == "🏆 ランキング":

    st.header("🏆 ジャンル別ランキング（全表示）")

    docs = list(db.collection("users").stream())
    users = [d.to_dict() for d in docs]

    for cat in RANKING_CATEGORIES:

        data = []

        for u in users:
            score = u.get("category_scores", {}).get(cat, 0)
            if score > 0:
                data.append({
                    "name": u.get("nickname", ""),
                    "score": score
                })

        if not data:
            continue

        data.sort(key=lambda x: x["score"], reverse=True)

        st.subheader(f"👑 {cat}")

        rank = 0
        prev_score = None

        for idx, r in enumerate(data):

            if r["score"] != prev_score:
                rank = idx + 1
                prev_score = r["score"]

            if rank > 10:
                break

            medal = (
                "🥇" if rank == 1 else
                "🥈" if rank == 2 else
                "🥉" if rank == 3 else "👤"
            )

            st.markdown(
                f"**{rank}位** {medal} **{r['name']}** ：{r['score']}点"
            )

        st.divider()