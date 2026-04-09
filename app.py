import os
import sqlite3
import json
import google.generativeai as genai
from dotenv import load_dotenv
from datetime import datetime, timezone
import re
import secrets
from functools import wraps
from werkzeug.utils import secure_filename
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image

from services.evaluation_service import (
    save_evaluation_from_json,
    get_latest_evaluations,
    get_ranking,
    get_evaluation_by_share_token,
    get_current_and_prev_by_share_token,
    verify_share_code,
    init_db_if_needed,
    CLEANING_THRESHOLD,   # ★ 50 をここから再利用
)

load_dotenv()

'''Flask アプリケーション本体の初期化'''
app = Flask(__name__)
#セッション暗号化用のシークレットキー
app.secret_key = "dev_secret_key_change_me"

'''パス・ディレクトリ設定'''
#プロジェクトのベースディレクトリ
BASE_DIR = os.path.dirname(__file__)

#SQLite のファイルパス
DB_PATH = os.path.join(BASE_DIR, "db", "database.sqlite")

#画像まわりのパス
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")


def ensure_dirs():
    """
    DBフォルダ・画像用フォルダをまとめて作成する関数。

    - db/           : SQLite ファイル格納用
    - tmp_uploads/  : 一時アップロード画像用
    - static/uploads: リサイズ済み画像用
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_db_connection():
    """
    SQLite コネクションを返すヘルパー関数。

    row_factory に sqlite3.Row を設定しているので、
    SELECT 結果を dict ライクに row["column"] で扱える。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

'''Flask 3.x での初期化処理（before_first_request 代替）'''
@app.before_request
def before_request():
    # Flask 3.x: before_first_request 廃止のため初回だけ初期化
    if not hasattr(app, "initialized"):
        ensure_dirs()
        init_db_if_needed(DB_PATH)
        app.initialized = True


'''ログイン必須デコレータ'''
def login_required(view_func):
    """
    ログインしていない場合は login 画面へリダイレクトさせるデコレータ。

    使用例：
    @app.route("/timeline")
    @login_required
    def timeline():
        ...
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            #ログインしていない場合はログイン画面へ
            #flash("ログインが必要です。", "error")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


'''認証まわり（新規登録・ログイン・ログアウト）'''
@app.route("/register", methods=["GET", "POST"])
def register():
    """
    新規登録画面。

    入力：
      - user_name : 表示名兼ID（@から始まる英数字15文字以内）
      - email     : ログイン用メールアドレス
      - password  : パスワード（ハッシュ化して保存）
    """
    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        #必須チェック
        if not user_name or not email or not password:
            flash("ユーザー名・メールアドレス・パスワードを入力してください。", "error")
            return render_template("register.html")
        
        #メールアドレスの簡易形式チェック（@が含まれているかのみ）
        if "@" not in email:
            flash("メールアドレスの形式が正しくありません。", "error")
            return render_template("register.html")

        #ユーザー名形式チェック (@ + 英数字1〜14文字 = 合計15文字以内)
        if not re.match(r"^@[A-Za-z0-9]{1,14}$", user_name):
            flash("ユーザー名は @ から始まる英数字15文字以内にしてください。", "error")
            return render_template("register.html")

        conn = get_db_connection()
        cur = conn.cursor()

        #user_name 重複チェック
        cur.execute("SELECT id FROM users WHERE user_name = ?", (user_name,))
        if cur.fetchone():
            conn.close()
            flash("このユーザー名は既に使用されています。", "error")
            return render_template("register.html")

        #email 重複チェック
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cur.fetchone():
            conn.close()
            flash("このメールアドレスは既に登録されています。", "error")
            return render_template("register.html")

        #パスワードをハッシュ化して保存
        password_hash = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (user_name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_name, email, password_hash, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()

        flash("登録が完了しました。ログインしてください。", "info")
        return redirect(url_for("login"))
    
    #GET の場合は単純にフォームを表示
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    ログイン画面。

    入力：
      - email
      - password
    成功時：
      - session["user_id"], session["user_name"], session["email"] をセット"""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, user_name, password_hash FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        conn.close()

        #メールアドレスが存在しない場合
        if not row:
            flash("メールアドレスまたはパスワードが正しくありません。", "error")
            return render_template("login.html")

        #パスワードチェック
        if not check_password_hash(row["password_hash"], password):
            flash("メールアドレスまたはパスワードが正しくありません。", "error")
            return render_template("login.html")

        #セッションにログイン情報を保存
        session["user_id"] = row["id"]
        session["user_name"] = row["user_name"]
        session["email"] = email
        flash("ログインしました。", "info")
        return redirect(url_for("rooms"))

    #GET の場合はログインフォームを表示
    return render_template("login.html")


@app.route("/logout")
def logout():
    """
    ログアウト処理。

    セッション情報をクリアしてログイン画面へ戻す。
    """
    session.clear()
    flash("ログアウトしました。", "info")
    return redirect(url_for("login"))


'''設定画面（ユーザー情報 & 公開設定 & パスワード変更）'''
@app.route("/settings", methods=["GET", "POST"])
def settings():
    """
    設定画面。

    ・ユーザー名の変更
    ・メールアドレスの変更
    ・パスワードの変更（任意）
    ・みんなの部屋への公開設定（ON/OFF）
    ・公開リンク送信先メールアドレスの設定
    """
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_name, email, password_hash, "
        "COALESCE(share_to_public, 0) AS share_to_public, "
        "share_email "
        "FROM users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        flash("ユーザー情報の取得に失敗しました。", "error")
        return redirect(url_for("rooms"))

    current_user_name = row["user_name"]
    current_email = row["email"]
    current_password_hash = row["password_hash"]
    current_share_to_public = row["share_to_public"]
    current_share_email = row["share_email"]

    if request.method == "POST":
        new_user_name = request.form.get("user_name", "").strip()
        new_email = request.form.get("email", "").strip()
        current_password = request.form.get("current_password", "").strip()
        new_password = request.form.get("new_password", "").strip()
        new_password_confirm = request.form.get("new_password_confirm", "").strip()
        share_to_public_flag = 1 if request.form.get("share_to_public") == "on" else 0
        share_email = request.form.get("share_email", "").strip()

        # ユーザー名形式チェック（@ + 英数字1〜14）
        if not re.match(r"^@[A-Za-z0-9]{1,14}$", new_user_name):
            conn.close()
            flash("ユーザー名は @ から始まる英数字15文字以内にしてください。", "error")
            return redirect(url_for("settings"))

        # メールアドレス簡易チェック
        if "@" not in new_email:
            conn.close()
            flash("メールアドレスの形式が正しくありません。", "error")
            return redirect(url_for("settings"))

        # 公開リンク送信先メールアドレスの形式チェック
        if share_email and "@" not in share_email:
            conn.close()
            flash("公開リンク送信先メールアドレスの形式が正しくありません。", "error")
            return redirect(url_for("settings"))

        # ユーザー名重複チェック（変更された場合のみ）
        if new_user_name != current_user_name:
            cur.execute("SELECT id FROM users WHERE user_name = ?", (new_user_name,))
            if cur.fetchone():
                conn.close()
                flash("このユーザー名は既に使用されています。", "error")
                return redirect(url_for("settings"))

        # メールアドレス重複チェック（変更された場合のみ）
        if new_email != current_email:
            cur.execute("SELECT id FROM users WHERE email = ?", (new_email,))
            if cur.fetchone():
                conn.close()
                flash("このメールアドレスは既に登録されています。", "error")
                return redirect(url_for("settings"))

        # パスワードを変更するかどうかの判定
        change_password = any([current_password, new_password, new_password_confirm])
        if change_password:
            # どれか空欄はNG
            if not current_password or not new_password or not new_password_confirm:
                conn.close()
                flash("パスワードを変更する場合は、現在・新しいパスワード・確認用をすべて入力してください。", "error")
                return redirect(url_for("settings"))

            # 現在パスワードが正しいか確認
            if not check_password_hash(current_password_hash, current_password):
                conn.close()
                flash("現在のパスワードが正しくありません。", "error")
                return redirect(url_for("settings"))

            # 新パスワードと確認用の一致チェック
            if new_password != new_password_confirm:
                conn.close()
                flash("新しいパスワードが一致しません。", "error")
                return redirect(url_for("settings"))

            new_password_hash = generate_password_hash(new_password)
        else:
            # 変更しない場合は既存のハッシュをそのまま使う
            new_password_hash = current_password_hash

        # users テーブルを更新（公開メールアドレスもここで保存）
        cur.execute(
            """
            UPDATE users
            SET user_name = ?, email = ?, password_hash = ?, share_to_public = ?, share_email = ?
            WHERE id = ?
            """,
            (new_user_name, new_email, new_password_hash, share_to_public_flag, share_email, user_id),
        )
        conn.commit()
        conn.close()

        # セッション内の user_name / email も更新
        session["user_name"] = new_user_name
        session["email"] = new_email

        flash("設定を更新しました。", "info")
        return redirect(url_for("settings"))

    # GET の場合は現在の値をテンプレートに渡して表示
    conn.close()
    return render_template(
        "settings.html",
        user_name=current_user_name,
        email=current_email,
        share_to_public=current_share_to_public,
        share_email=current_share_email,
    )


'''画面：タイムライン・ランキング'''
@app.route("/")
@login_required
def root():
    """
    ルートパス。
    ログイン済みであればタイムラインにリダイレクトする。
    """
    return redirect(url_for("rooms"))


@app.route("/timeline")
@login_required
def timeline():
    """
    MYタイムライン。
    evaluations: 自分の評価全件（新しい順）
    ベストスコアもここで算出。
    """
    user_id = session["user_id"]
    evaluations, _, _, _ = get_latest_evaluations(DB_PATH, user_id)

    # ベストスコアの算出
    best_score = None
    best_level = None
    best_captured_at = None
    if evaluations:
        # score の最大値を持つ評価を 1 件探す
        best_ev = max(evaluations, key=lambda ev: ev["score"])
        best_score = best_ev["score"]
        best_level = best_ev["level"]
        best_captured_at = best_ev["captured_at"]

    return render_template(
        "timeline.html",
        evaluations=evaluations,
        #latest_score=latest_score,
        #latest_level=latest_level,
        #latest_updated_at=latest_updated_at,
        best_score=best_score,
        best_level=best_level,
        best_captured_at=best_captured_at,
    )


@app.route("/ranking")
@login_required
def ranking():
    """
    MYランキング画面。

    - top5  : スコア上位5件
    - lowest: スコア最下位1件
    """
    user_id = session["user_id"]
    top5, lowest = get_ranking(DB_PATH, user_id)
    return render_template("ranking.html", top5=top5, lowest=lowest)


'''MiseRoom（公開ONユーザーの評価を一覧表示）'''
@app.route("/rooms")
@login_required
def rooms():
    """
    MiseRoom

    - share_to_public = 1 のユーザーの Evaluations を新着順に一覧表示
    - 全体の最低スコア1件も追加して表示（最新リストに無ければ追加）
    """
    conn = get_db_connection()
    cur = conn.cursor()

    #公開ONユーザーの評価（新しい順）
    cur.execute(
        """
        SELECT e.*, u.user_name
        FROM evaluations e
        JOIN users u ON e.user_id = u.id
        WHERE u.share_to_public = 1
        ORDER BY e.captured_at DESC
        """
    )
    latest_rows = cur.fetchall()

    evaluations = []
    for r in latest_rows:
        evaluations.append({
            "id": r["id"],
            "user_id": r["user_id"],
            "image_path": r["image_path"],
            "captured_at": r["captured_at"],
            "created_at": r["created_at"],
            "score": r["score"],
            "level": r["level"],
            "comment": r["comment"],
            "advice": r["advice"],
            "status": r["status"],
            "due_at": r["due_at"],
            "is_for_ranking": r["is_for_ranking"],
            "user_name": r["user_name"],
        })

    #全体の最低スコア1件（公開ONユーザーの中から）
    cur.execute(
        """
        SELECT e.*, u.user_name
        FROM evaluations e
        JOIN users u ON e.user_id = u.id
        WHERE u.share_to_public = 1
        ORDER BY e.score ASC, e.captured_at DESC
        """
    )
    lowest_row = cur.fetchone()
    conn.close()

    if lowest_row:
        lowest = {
            "id": lowest_row["id"],
            "user_id": lowest_row["user_id"],
            "image_path": lowest_row["image_path"],
            "captured_at": lowest_row["captured_at"],
            "created_at": lowest_row["created_at"],
            "score": lowest_row["score"],
            "level": lowest_row["level"],
            "comment": lowest_row["comment"],
            "advice": lowest_row["advice"],
            "status": lowest_row["status"],
            "due_at": lowest_row["due_at"],
            "is_for_ranking": lowest_row["is_for_ranking"],
            "user_name": lowest_row["user_name"],
        }
        #最新一覧に含まれていなければ追加（最大で +1 件）
        ids = {e["id"] for e in evaluations}
        if lowest["id"] not in ids:
            evaluations.append(lowest)

    return render_template("rooms.html", evaluations=evaluations)

'''他ユーザーの公開タイムライン（/user/@xxx）'''
@app.route("/user/<user_name>")
@login_required
def public_timeline(user_name):
    """
    MiseRoom ONユーザーの公開タイムライン表示。

    - URL: /user/@xxx
    - share_to_public が 1 のユーザーのみ表示可能
    """
    #対象ユーザーを取得
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_name, share_to_public FROM users WHERE user_name = ?",
        (user_name,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        flash("ユーザーが見つかりません。", "error")
        return redirect(url_for("rooms"))

    #公開設定がOFFなら見せない
    if row["share_to_public"] == 0:
        conn.close()
        flash("このユーザーのタイムラインは公開されていません。", "error")
        return redirect(url_for("rooms"))

    target_user_id = row["id"]
    target_user_name = row["user_name"]
    conn.close()

    #そのユーザーの評価一覧を取得
    evaluations, latest_score, latest_level, latest_updated_at = get_latest_evaluations(
        DB_PATH, target_user_id
    )
    #評価カードで「部屋主：@xxx」を正しく表示させるために user_name を埋める
    for ev in evaluations:
        ev["user_name"] = target_user_name

    return render_template(
        "user_timeline.html",
        target_user_name=target_user_name,
        evaluations=evaluations,
    )


'''画像リサイズ処理'''
def resize_and_save_image(file_storage, captured_at_str: str, user_name: str) -> str:
    """
    アップロードされた画像をメモリ上でリサイズし、
    直接 static/uploads に保存する。

    ファイル名は「ユーザー名 + 撮影日時 + ランダム文字列」をベースに作成。

    戻り値:
        DB に保存する用パス（例: "static/uploads/@user_20251124_211200_a1b2c3d4.jpg"）
    """
    # 撮影日時の形式チェック & datetime 変換
    try:
        captured_dt = datetime.strptime(captured_at_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("撮影日時は YYYY-MM-DD HH:MM:SS 形式で入力してください。")
    
    # file_storage から直接 Pillow で読み込む（tmpファイルは使わない）
    # 念のため先頭にシーク

    file_storage.stream.seek(0)
    img = Image.open(file_storage.stream)
    img = img.convert("RGB")
    # 長辺 1024px に収まるよう縮小（アスペクト比維持）
    img.thumbnail((1024, 1024), Image.LANCZOS)

    # user_name + 撮影日時 + ランダム文字列からファイル名生成
    random_suffix = secrets.token_hex(4)
    safe_user_name = user_name.replace(os.sep, "_")
    final_name = f"{safe_user_name}_{captured_dt.strftime('%Y%m%d_%H%M%S')}_{random_suffix}.jpg"

    final_rel_path = os.path.join("static", "uploads", final_name)
    final_abs_path = os.path.join(BASE_DIR, final_rel_path)
    os.makedirs(os.path.dirname(final_abs_path), exist_ok=True)

    # JPEG 品質 80 で保存
    img.save(final_abs_path, "JPEG", quality=80)
    # DB に保存する相対パスを返す
    return final_rel_path

'''部屋投稿（AI評価付き）'''
def get_prev_latest_info(user_id: int) -> tuple[str | None, int | None]:
    """
    直近（前回）の評価の (画像absolute path, スコア) を返す。
    1件も無い場合は (None, None)。
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT score, image_path
        FROM evaluations
        WHERE user_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None, None

    prev_score = int(row["score"])
    prev_abs = os.path.join(BASE_DIR, row["image_path"])

    if not os.path.exists(prev_abs):
        return None, None

    return prev_abs, prev_score

''' プロンプト（単体 / 比較） '''
PROMPT_COMPARE = """
あなたは「部屋の綺麗さ評価アシスタント」です。
入力には必ず「current（今回）」の画像が含まれます。
条件によっては「previous（前回）」の画像と、
「previous_score（前回スコア）」が与えられます。

まず以下を厳密に判定してください：
- current と previous が「同じ部屋（同一空間）」かどうか
  ※ 家具配置・壁・床・窓など固定要素を優先し、
     散らかった物（可動物）だけでは判断しないこと。

【分岐ルール（厳守）】
- 別の部屋 / 判断不能：
  → previous と previous_score は無視し、
     current の状態のみで通常評価を行う。
- 同じ部屋と高確度で判断できる場合のみ：
  → score は previous画像の{previous_score}点を元にcurrent画像を評価する。
  → comment または advice のどちらか一方に、
     「前回スコア {previous_score} 点と比べて〜」という
     形の比較文を必ず1つ含める

次の10個の観点を内部的な評価軸として用いながら、
最終的に「総合スコア」「レベル」「コメント」「アドバイス」だけを JSON で返してください。

内部で考慮する評価項目（すべて 0〜100 点で頭の中で評価すること）:
• clutter: 床や机に物が散乱している度合い（多いほど点数低い）
• cleanliness: ゴミ・汚れ・ホコリの見え方（汚いほど点数低い）
• organization: 物の配置の整い具合（整っているほど高い）
• floor_visibility: 床の可視範囲（広いほど高い）
• trash_presence: ゴミ袋・空き缶・食べ残し等の存在（多いほど低い）
• laundry_presence: 洗濯物・衣類が放置されている度合い（多いほど低い）
• desk_orderliness: 机周辺の整理整頓（整っているほど高い）
• bed_state: ベッドが整っているか（寝具の乱れ・放置物）
• pathways_clearance: 部屋の動線が確保されているか（歩けるスペースがあるか）
• hygiene_risk: カビ・飲食物放置など衛生的リスク（高いほど低い点）

総合スコア:
• 上記の観点を総合的に判断し、0〜100 点の整数で「score」として出力すること。

レベル分類:
• 90〜100: "very_clean"
• 70〜89: "clean"
• 50〜69: "normal"
• 30〜49: "messy"
• 0〜29: "very_messy"

出力条件（重要）:
• 必ず JSON のみ（説明文・前置き・補足は禁止）
• 全フィールドを必ず含める
• score は整数、他は文字列

出力フォーマット（厳守）:
{
  "score": 0,
  "level": "string",
  "comment": "string",
  "advice": "string"
}
"""


''' Gemini 呼び出し（比較対応） '''
def call_ai_evaluation_api(
    image_abs_path: str,
    prev_image_abs_path: str | None = None,
    prev_score: int | None = None
) -> str:
    """
    画像を Gemini API に送り、4項目JSONを生成する。
    - 常に PROMPT_COMPARE を使用
    - previous が無い場合は AI が通常評価にフォールバックする
    """

    dummy = {
        "score": 50,
        "level": "normal",
        "comment": "AI評価が利用できないためダミー評価を返しています。",
        "advice": "まずは床の物を減らすことから始めてみましょう。"
    }

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY が未設定です。")
        return json.dumps(dummy, ensure_ascii=False)

    genai.configure(api_key=api_key)

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={"response_mime_type": "application/json"}
        )

        if not os.path.exists(image_abs_path):
            print("⚠️ current画像が存在しません")
            return json.dumps(dummy, ensure_ascii=False)

        with open(image_abs_path, "rb") as f:
            current_bytes = f.read()

        parts = [
            PROMPT_COMPARE,
            "current:",
            {"mime_type": "image/jpeg", "data": current_bytes},
        ]

        # ★ 前回があれば必ず2枚送る
        if prev_image_abs_path and prev_score is not None and os.path.exists(prev_image_abs_path):
            with open(prev_image_abs_path, "rb") as f:
                prev_bytes = f.read()

            parts += [
                f"previous_score: {prev_score}",
                "previous:",
                {"mime_type": "image/jpeg", "data": prev_bytes},
            ]

        response = model.generate_content(parts)

        raw_text = (response.text or "").strip()

        # ```json ``` 除去
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1]
            if raw_text.endswith("```"):
                raw_text = raw_text.rsplit("\n", 1)[0]

        parsed = json.loads(raw_text)

        result = {
            "score": int(parsed.get("score", dummy["score"])),
            "level": str(parsed.get("level", dummy["level"])),
            "comment": str(parsed.get("comment", dummy["comment"])),
            "advice": str(parsed.get("advice", dummy["advice"])),
        }
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        print(f"⚠️ Gemini API エラー: {e}")
        return json.dumps(dummy, ensure_ascii=False)

'''部屋投稿（AI評価付き）'''
@app.route("/post_room", methods=["GET", "POST"])
@login_required
def post_room():
    """
    Web から部屋画像を投稿し、
    サーバー側で Gemini API を叩いて自動評価したうえで
    evaluations に登録する画面。

    入力:
      - 撮影日時（YYYY-MM-DD HH:MM:SS）
      - 部屋画像ファイル（アップロード or カメラ撮影）

    処理:
      1. 画像をリサイズして static/uploads に保存（resize_and_save_image）
      2. 保存した画像パスから絶対パスを作り、Gemini API で評価JSON取得
      3. save_evaluation_from_json で evaluations に登録（画像は evaluation_id.jpg にリネーム）
    """
    if request.method == "POST":
        captured_at = request.form.get("captured_at", "").strip()
        image_file = request.files.get("image")

        # 必須チェック
        if not captured_at or not image_file:
            flash("撮影日時と部屋画像は必須です。", "error")
            return render_template("post_room.html")

        # 画像リサイズ＆保存
        try:
            image_path = resize_and_save_image(
                image_file,
                captured_at,
                session.get("user_name", "")
            )
        except ValueError as e:
            flash(str(e), "error")
            return render_template("post_room.html")

        user_id = session["user_id"]

        # ★前回スコア<50なら前回画像を取得
        prev_abs, prev_score = get_prev_latest_info(user_id)

        # 保存した画像をもとに Gemini で評価JSON取得
        try:
            abs_path = os.path.join(BASE_DIR, image_path)
            raw_json = call_ai_evaluation_api(abs_path,prev_image_abs_path=prev_abs,prev_score=prev_score)
        except Exception as e:
            flash(f"AI評価の生成に失敗しました: {e}", "error")
            return render_template("post_room.html")

        # 評価を DB に登録（画像は <evaluation_id>.jpg にリネームされる）
        try:
            save_evaluation_from_json(
                DB_PATH,
                user_id,
                image_path,
                captured_at,
                raw_json,
                rename_to_eval_id=True,
            )
        except ValueError as ve:
            flash(str(ve), "error")
            return render_template("post_room.html")
        except Exception as e:
            flash(f"評価登録中にエラーが発生しました: {e}", "error")
            return render_template("post_room.html")

        flash("部屋の評価を投稿しました。")
        return redirect(url_for("timeline"))
    # GET の場合はフォーム表示
    return render_template("post_room.html")


'''評価JSONの手動登録（開発用）'''
@app.route("/admin/add_evaluation", methods=["GET", "POST"])
@login_required
def admin_add_evaluation():
    """
    評価JSONの手動登録（開発用画面）。

    1ページで「画像アップロード → リサイズ → 評価登録」まで完了させる。

    入力項目:
      - 撮影日時（YYYY-MM-DD HH:MM:SS）
      - 部屋画像ファイル
      - 評価JSON（生成AIの出力をコピペ）
    """
    if request.method == "POST":
        captured_at = request.form.get("captured_at", "").strip()
        raw_json = request.form.get("json", "").strip()
        image_file = request.files.get("image")

        #必須チェック
        if not captured_at or not raw_json or not image_file:
            flash("撮影日時・画像ファイル・評価JSONはすべて必須です。", "error")
            return render_template("admin_add_evaluation.html")

        #JSON 妥当性チェック
        try:
            json.loads(raw_json)
        except json.JSONDecodeError:
            flash("評価JSONの形式が不正です。", "error")
            return render_template("admin_add_evaluation.html")

        #画像をリサイズして保存 → パス取得
        try:
            image_path = resize_and_save_image(
                image_file,
                captured_at,
                session.get("user_name", "")
            )
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin_add_evaluation.html")

        user_id = session["user_id"]
        
        # 評価を DB に保存
        #画像を <evaluation_id>.jpg にリネーム & image_path を更新する
        save_evaluation_from_json(
            DB_PATH,
            user_id,
            image_path,
            captured_at,
            raw_json,
            rename_to_eval_id=True,
        )

        flash("評価を登録しました。")
        return redirect(url_for("timeline"))

    #GET の場合はフォーム表示のみ
    return render_template("admin_add_evaluation.html")


'''公開URL（家族向け・常に非ログインUI） /public/<token>'''
@app.route("/public/<token>", methods=["GET", "POST"])
def public_view(token):
    """
    家族などが見る公開URL用画面。

    - GET : ワンタイムコード入力フォームを表示
    - POST: コード検証 → OKなら評価を表示、NGならエラーメッセージ表示

    public_mode=True をテンプレートに渡すことで、
    base.html 側で「非ログイン用ヘッダー表示」を行う。
    """
    if request.method == "POST":
        #form 側では name="one_time_code" で送信される前提
        code = request.form.get("one_time_code", "").strip()
        ok, message = verify_share_code(DB_PATH, token, code)
        if not ok:
            flash(message, "error")
            return render_template("public_code.html", token=token, public_mode=True)

        # ★今回＋前回を取得
        current, prev = get_current_and_prev_by_share_token(DB_PATH, token)
        if not current:
            return render_template("public_invalid.html", public_mode=True)

        return render_template(
            "public_view.html",
            current=current,
            prev=prev,
            public_mode=True
        )

    return render_template("public_code.html", token=token, public_mode=True)


'''アカウント削除'''
@app.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    """
    アカウント削除処理。

    - users レコード削除
    - evaluations / share_links の関連データも削除
    - セッションをクリアして新規登録画面へ
    """
    user_id = session["user_id"]
    conn = get_db_connection()
    cur = conn.cursor()

    #該当ユーザーの evaluations を取得
    cur.execute("SELECT id FROM evaluations WHERE user_id = ?", (user_id,))
    eval_ids = [row["id"] for row in cur.fetchall()]

    if eval_ids:
        #evaluations に紐づく share_links を削除
        cur.executemany(
            "DELETE FROM share_links WHERE evaluation_id = ?",
            [(eid,) for eid in eval_ids],
        )
        #evaluations 本体も削除
        cur.execute("DELETE FROM evaluations WHERE user_id = ?", (user_id,))

    #users レコード削除
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    #セッションクリア
    session.clear()
    flash("アカウントを削除しました。", "info")
    return redirect(url_for("register"))


# =========================
# API: upload_from_pi  ★比較対応
# =========================
@app.route("/api/upload_from_pi", methods=["POST"])
def api_upload_from_pi():
    """
    Raspberry Pi など外部デバイスからの自動アップロード用API。

    期待する入力（multipart/form-data）:
      - user_id: 評価対象ユーザーID（必須）
      - captured_at: 撮影日時 "YYYY-MM-DD HH:MM:SS" （任意。省略時はサーバー現在時刻）
      - image: 画像ファイル（必須。ラズパイ側でリサイズ済みJPEG想定）

    処理の流れ:
      1. user_id の存在チェック
      2. static/uploads/ に一旦一時ファイル名で保存
      3. 画像を元にサーバー側で AI API を叩いて評価JSONを取得
      4. save_evaluation_from_json() に raw_json を渡して evaluations に登録
         - rename_to_eval_id=True により画像ファイルが <evaluation_id>.jpg にリネームされる
      5. evaluation_id, score, status を JSON で返す
    """
    
    # 1. パラメータ取得
    user_id = request.form.get("user_id", "").strip()
    captured_at = request.form.get("captured_at", "").strip()
    image_file = request.files.get("image")

    if not user_id or not image_file:
        return jsonify({"error": "user_id と image は必須です。"}), 400
    
    # captured_at 未指定ならサーバー側現在時刻を使用
    #if not captured_at:
    #   captured_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # user_id が整数かチェック

    try:
        user_id_int = int(user_id)
    except ValueError:
        return jsonify({"error": "user_id は整数で指定してください。"}), 400

    # 2. user の存在チェック
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE id = ?", (user_id_int,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "指定された user_id のユーザーが存在しません。"}), 400

    #3.画像を static/uploads に一旦保存
    safe_name = secure_filename(image_file.filename or "upload.jpg")
    #拡張子を落としておく（後で evaluation_id.jpg にリネームされるため）
    base_name, ext = os.path.splitext(safe_name)
    random_suffix = secrets.token_hex(4)
    tmp_filename = f"user{user_id_int}_{random_suffix}{ext or '.jpg'}"

    rel_path = os.path.join("static", "uploads", tmp_filename)
    abs_path = os.path.join(BASE_DIR, rel_path)
    image_file.save(abs_path)

    prev_abs, prev_score = get_prev_latest_info(user_id_int)

    #4.サーバー側で AI API を叩いて評価JSONを取得
    try:
        raw_json = call_ai_evaluation_api(abs_path, prev_image_abs_path=prev_abs, prev_score=prev_score)
        #妥当性チェック（最低限 JSON としてパースできるかだけ確認）
        json.loads(raw_json)
    except Exception as e:
        #評価生成に失敗した場合、保存した画像はそのまま残しておくか、
        #必要であればここで削除してもよい
        return jsonify({"error": f"AI評価の生成に失敗しました: {e}"}), 500

    #5.評価をDBに登録（画像は <evaluation_id>.jpg にリネームされる）
    try:
        save_evaluation_from_json(
            DB_PATH,
            user_id_int,
            rel_path,
            captured_at,
            raw_json,
            rename_to_eval_id=True
        )
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": f"評価登録中にエラーが発生しました: {e}"}), 500

    #save_evaluation_from_json 内で new_eval_id を返していないため、
    #「直近の評価ID」を取得することで評価IDを返す。
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, score, status, comment, advice, due_at
        FROM evaluations
        WHERE user_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (user_id_int,),
    )
    latest = cur.fetchone()
    conn.close()

    if latest is None:
        return jsonify({"error": "評価は保存されたはずですが、取得に失敗しました。"}), 500

    return jsonify({
        "evaluation_id": latest["id"],
        "user_id": user_id_int,
        "score": latest["score"],
        "status": latest["status"],
        "comment": latest["comment"],
        "advice": latest["advice"],
        "due_at": latest["due_at"],
    }), 200


if __name__ == "__main__":
    app.run(debug=True)
