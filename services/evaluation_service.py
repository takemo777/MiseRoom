import os
import sqlite3
import json
from datetime import datetime, timedelta, timezone
import secrets
import smtplib
from email.mime.text import MIMEText


'''設定値（許容スコア・猶予時間・ステータス定数）'''

#許容スコア
CLEANING_THRESHOLD = 50

#猶予時間
#「撮影時刻 + この分数」までを掃除の期限として表示する
CLEANING_GRACE_MINUTES = 4320

STATUS_NEED = "need_cleaning"      #要掃除（期限付き）
STATUS_CLEANED = "cleaned"         #掃除済み
STATUS_OVERDUE = "overdue"         #期限切れ（家族向け公開対象）

'''DB接続まわり'''

def get_connection(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db_if_needed(db_path: str):
    #db/ ディレクトリがなければ作成
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = get_connection(db_path)
    cur = conn.cursor()

    #users テーブル
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name      TEXT NOT NULL UNIQUE,   -- @から始まるユーザー名（表示用・一意）
            email          TEXT NOT NULL UNIQUE,   -- ログイン用メールアドレス（ユニーク）
            password_hash  TEXT NOT NULL,          -- パスワードのハッシュ値
            created_at     TEXT NOT NULL,          -- 作成日時（文字列）
            share_to_public INTEGER NOT NULL DEFAULT 0, -- みんなの部屋に公開するかどうか（0/1）
            share_email    TEXT                    -- 公開リンク送信先メールアドレス（任意）
        );
        """
    )

    # 既存DB向け：share_email 列がなければ追加
    cur.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in cur.fetchall()]
    if "share_email" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN share_email TEXT")

    #evaluations テーブル
    #部屋の評価結果を1レコード1評価で保持
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluations (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id              INTEGER NOT NULL,  -- 評価の対象ユーザーID（users.id）
            image_path           TEXT NOT NULL,     -- リサイズ後の画像パス（static/uploads/...）
            captured_at          TEXT NOT NULL,     -- 撮影日時（ユーザー入力）
            created_at           TEXT NOT NULL,     -- DB登録日時（UTC）
            score                INTEGER NOT NULL,  -- 総合スコア（0〜100）
            level                TEXT NOT NULL,     -- レベル文字列（very_clean / clean / normal / messy / very_messy）
            comment              TEXT NOT NULL,     -- コメント（短い所感）
            advice               TEXT NOT NULL,     -- アドバイス（改善提案）
            status               TEXT NOT NULL,     -- 状態（need_cleaning / cleaned / overdue）
            due_at               TEXT,              -- 掃除期限（score < 閾値のときのみ）
            is_for_ranking       INTEGER NOT NULL DEFAULT 0, -- 今後ランキング専用のフラグとして利用予定
            raw_json             TEXT NOT NULL,     -- 元の評価JSONをそのまま保存
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )

    #share_links テーブル
    #期限切れカード（overdue）用の公開URLを管理
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            evaluation_id   INTEGER NOT NULL,   -- 公開対象となる evaluations.id
            token           TEXT NOT NULL UNIQUE, -- 公開URLに含めるランダム文字列
            one_time_code   TEXT NOT NULL,     -- ワンタイムコード（6桁）
            expires_at      TEXT NOT NULL,     -- 公開URLの有効期限（UTC）
            remaining_uses  INTEGER NOT NULL DEFAULT 1, -- 残り使用回数
            created_at      TEXT NOT NULL,     -- 発行日時（UTC）
            FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
        );
        """
    )

    conn.commit()
    conn.close()


#JSON保存関連（評価JSONのパースと保存）
def parse_evaluation_json(raw_json: str) -> dict:
    """
    評価JSON（部屋の綺麗さ評価アシスタントの出力）をパースして、
    DBに保存しやすい形の dict に変換する。

    想定しているJSON形式：
    {
      "score": 0,
      "level": "string",
      "comment": "string",
      "advice": "string"
    }
    """
    data = json.loads(raw_json)

    return {
        "score": int(data["score"]),
        "level": str(data["level"]),
        "comment": str(data["comment"]),
        "advice": str(data["advice"]),
    }


def determine_status_and_due(score: int, captured_at_str: str) -> tuple[str, str | None]:
    """
    スコアと撮影日時から status と due_at(文字列) を決める。

    ロジック：
    - score >= CLEANING_THRESHOLD → cleaned（期限なし）
    - score <  CLEANING_THRESHOLD → need_cleaning（猶予時間ぶんの期限付き）
    """
    if score >= CLEANING_THRESHOLD:
        #閾値以上なら「掃除済み」として扱い、期限は持たない
        return STATUS_CLEANED, None

    #閾値未満の場合：撮影日時に猶予時間を足して due_at を決定
    captured_dt = datetime.strptime(captured_at_str, "%Y-%m-%d %H:%M:%S")
    due_dt = captured_dt + timedelta(minutes=CLEANING_GRACE_MINUTES)
    return STATUS_NEED, due_dt.strftime("%Y-%m-%d %H:%M:%S")

def send_overdue_mail(db_path: str, user_id: int, token: str, one_time_code: str, expires_at_str: str):
    """
    掃除期限切れ（overdue）用の公開URLとワンタイムコードをメール送信する。

    宛先:
      - users.share_email が設定されていればそちら
      - 未設定なら users.email
      - 両方なければ何もしない
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_name, email, share_email FROM users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        print(f"⚠️ send_overdue_mail: ユーザーが見つかりません: user_id={user_id}")
        return

    user_name = row["user_name"]
    login_email = row["email"]
    share_email = row["share_email"]

    to_addr = share_email
    if not to_addr:
        print("公開リンク送信先メールアドレスが未設定のため、メール送信を行いません。")
        return

    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)
 
    if not smtp_user or not smtp_pass:
        print("SMTP_USER / SMTP_PASSWORD が設定されていないため、メール送信をスキップします。")
        return

    base_url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5000")
    share_url = f"{base_url}/public/{token}"

    subject = "【MiseRoom】お部屋の状況確認リンクのお知らせ"
    body = f"""{user_name} さんの部屋の掃除期限が過ぎました。

以下のURLから部屋の様子を確認できます。

URL: {share_url}
ワンタイムコード: {one_time_code}
有効期限: {expires_at_str}

※ このリンクは1回だけ有効です。
"""

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)

        print(f"[INFO] 共有リンクを {to_addr} に送信しました。")

    except Exception as e:
        print(f"⚠️ 共有リンクのメール送信に失敗しました: {e}")


def save_evaluation_from_json(
    db_path: str,
    user_id: int,
    image_path: str,
    captured_at: str,
    raw_json: str,
    rename_to_eval_id: bool = False,
):
    """
    評価JSONをパースし、evaluations テーブルに保存するメイン処理。

    1. JSON文字列をパースしてスコア情報を取り出す
    2. スコアと撮影日時から status / due_at を決める
    3. evaluations にINSERT
    4. 同じユーザーの直前の評価（前回）と比較して、
       - 前回 <閾値 & 今回 <閾値 → 前回を overdue にし、公開URL(share_links)を生成
       - それ以外→前回を cleaned に更新（掃除済みとして扱う）
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    #1. JSONパース
    parsed = parse_evaluation_json(raw_json)
    score = parsed["score"]

    #2. status / due_at 決定
    status, due_at = determine_status_and_due(score, captured_at)

    #3. 同じユーザーの直近1件を取得（「前回」の評価）
    cur.execute(
        """
        SELECT id, score, status
        FROM evaluations
        WHERE user_id = ?
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    last_row = cur.fetchone()

    #4. 今回の評価をINSERT
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO evaluations (
            user_id, image_path, captured_at, created_at,
            score, level,
            comment, advice,
            status, due_at, is_for_ranking, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            image_path,
            captured_at,
            now_str,
            parsed["score"],
            parsed["level"],
            parsed["comment"],
            parsed["advice"],
            status,
            due_at,
            0,  # is_for_ranking（とりあえず0）
            raw_json,
        ),
    )
    new_eval_id = cur.lastrowid
    
    # ★ ここから追加: 画像ファイルを <evaluation_id>.jpg にリネームする処理
    if rename_to_eval_id:
        try:
            # プロジェクトルート (= services/ の1つ上)
            base_dir = os.path.dirname(os.path.dirname(__file__))

            # 保存済みの元ファイル（相対パス "static/uploads/..." 前提）
            if os.path.isabs(image_path):
                old_abs_path = image_path
            else:
                old_abs_path = os.path.join(base_dir, image_path)

            # 新しいファイル名 <id>.jpg
            new_filename = f"{new_eval_id}.jpg"
            new_rel_path = os.path.join("static", "uploads", new_filename)
            new_abs_path = os.path.join(base_dir, new_rel_path)

            # 実ファイル名を変更
            os.replace(old_abs_path, new_abs_path)

            # DB の image_path を新パスに更新
            cur.execute(
                "UPDATE evaluations SET image_path = ? WHERE id = ?",
                (new_rel_path, new_eval_id),
            )

            # ログ用（任意）
            print(f"[INFO] image renamed: {image_path} -> {new_rel_path}")

        except OSError as e:
            # リネームに失敗してもアプリは止めない（ログだけ出す）
            print(f"[WARN] 画像リネームに失敗しました: {e}")

        #5. 前回が存在する場合、前回スコアとの比較ロジック
    if last_row is not None:
        prev_id = last_row["id"]
        prev_score = last_row["score"]
        prev_status = last_row["status"]

        # 前回 <閾値 かつ 今回 <閾値 → 「掃除期限を守れなかった」とみなし、
        # 今回の評価(new_eval_id)を公開対象として share_links を発行する。
        if prev_score < CLEANING_THRESHOLD and score < CLEANING_THRESHOLD:
            # 前回の status を overdue に変更（タイムライン上では前回が「期限切れ」として表示される）
            cur.execute(
                "UPDATE evaluations SET status = ? WHERE id = ?",
                (STATUS_OVERDUE, prev_id),
            )

            # 公開用リンクの作成（今回の評価ID = new_eval_id を紐づける）
            token = secrets.token_urlsafe(16)  # 公開URLに使うランダムトークン
            one_time_code = f"{secrets.randbelow(1000000):06d}"  # 6桁のワンタイムコード
            JST = timezone(timedelta(hours=9))

            # 有効期限は発行から2日後まで
            expires_at = (
                datetime.now(JST) + timedelta(days=2)
            ).strftime("%Y-%m-%d %H:%M:%S")

            cur.execute(
                """
                INSERT INTO share_links (
                    evaluation_id, token, one_time_code, expires_at,
                    remaining_uses, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_eval_id, token, one_time_code, expires_at, 1, now_str),
            )

            # 共有リンクをメール送信（SMTPが未設定なら内部でスキップ）
            try:
                send_overdue_mail(
                    db_path=db_path,
                    user_id=user_id,
                    token=token,
                    one_time_code=one_time_code,
                    expires_at_str=expires_at,
                )
            except Exception as e:
                # メール失敗はアプリ致命傷ではないのでログだけ
                print(f"⚠️ 共有リンクメール送信中にエラー: {e}")

            # デバッグ用ログ（任意）
            print("=== 期限切れカードが発生しました ===")
            print(f"公開URL: /public/{token}")
            print(f"ワンタイムコード: {one_time_code}")
            print("================================")

        else:
            # 上記以外の場合は「前回の掃除期限は守られた」とみなし、
            # 前回を cleaned にして due_at をクリアする。
            cur.execute(
                "UPDATE evaluations SET status = ?, due_at = NULL WHERE id = ?",
                (STATUS_CLEANED, prev_id),
            )

    conn.commit()
    conn.close()
    

'''タイムライン・ランキング取得'''

def normalize_evaluation_row(row: sqlite3.Row) -> dict:

    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "image_path": row["image_path"],
        "captured_at": row["captured_at"],
        "created_at": row["created_at"],
        "score": row["score"],
        "level": row["level"],
        "comment": row["comment"],
        "advice": row["advice"],
        "status": row["status"],
        "due_at": row["due_at"],
        "is_for_ranking": row["is_for_ranking"],
    }


def get_latest_evaluations(db_path: str, user_id: int):
    
    #タイムライン用（MYタイムライン・公開タイムライン共通）：
    #指定ユーザーの全評価を新しい順に取得する。
    
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM evaluations
        WHERE user_id = ?
        ORDER BY captured_at DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()

    evaluations = [normalize_evaluation_row(r) for r in rows]

    latest_score = None
    latest_level = None
    latest_updated_at = None
    if evaluations:
        latest_score = evaluations[0]["score"]
        latest_level = evaluations[0]["level"]
        latest_updated_at = evaluations[0]["captured_at"]

    conn.close()
    return evaluations, latest_score, latest_level, latest_updated_at


def get_ranking(db_path: str, user_id: int):
    
    #ランキング画面用のデータ取得。
    #- スコア上位5件
    #- 最低スコア1件（ワースト1）
    conn = get_connection(db_path)
    cur = conn.cursor()

    #スコア上位5件
    cur.execute(
        """
        SELECT *
        FROM evaluations
        WHERE user_id = ?
        ORDER BY score DESC, captured_at DESC
        LIMIT 5
        """,
        (user_id,),
    )
    top5_rows = cur.fetchall()
    top5 = [normalize_evaluation_row(r) for r in top5_rows]

    #最低スコア1件（ワースト1）
    cur.execute(
        """
        SELECT *
        FROM evaluations
        WHERE user_id = ?
        ORDER BY score ASC, captured_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    lowest_row = cur.fetchone()
    lowest = normalize_evaluation_row(lowest_row) if lowest_row else None

    conn.close()
    return top5, lowest


def get_current_and_prev_by_share_token(db_path: str, token: str) -> tuple[dict | None, dict | None]:
    """
    share_links.token から「今回の評価(current)」を取得し、
    同じ user_id の「直前の評価(prev)」も取得して返す。

    戻り値: (current, prev)
      - current が無ければ (None, None)
      - prev が無ければ (current, None)
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    # current（share_links が指している evaluation）
    cur.execute(
        """
        SELECT e.*
        FROM evaluations e
        JOIN share_links s ON e.id = s.evaluation_id
        WHERE s.token = ?
        """,
        (token,),
    )
    current_row = cur.fetchone()
    if not current_row:
        conn.close()
        return None, None

    current = normalize_evaluation_row(current_row)

    # prev（同一ユーザーの直前）
    cur.execute(
        """
        SELECT *
        FROM evaluations
        WHERE user_id = ?
          AND (captured_at < ? OR (captured_at = ? AND id < ?))
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (current["user_id"], current["captured_at"], current["captured_at"], current["id"]),
    )
    prev_row = cur.fetchone()
    conn.close()

    prev = normalize_evaluation_row(prev_row) if prev_row else None
    return current, prev


'''公開URL（家族向けワンタイム共有）'''

def get_evaluation_by_share_token(db_path: str, token: str) -> dict | None:
    
    #share_links.token から evaluations を1件取得する。
    #公開URL /public/<token> にアクセスされたとき、
    #有効な token であれば紐づく evaluations レコードを返す。
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT e.*
        FROM evaluations e
        JOIN share_links s
          ON e.id = s.evaluation_id
        WHERE s.token = ?
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return normalize_evaluation_row(row)


def verify_share_code(db_path: str, token: str, code: str) -> tuple[bool, str]:
    """
    公開URL + ワンタイムコードの検証を行う。

    - token（URLパラメータ）で share_links を検索
    - 有効期限 / 残り使用回数 / コード一致 をチェック
    - OKなら remaining_uses を1減らし、(True, "") を返す
    - NGなら (False, "エラーメッセージ") を返す
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, one_time_code, expires_at, remaining_uses
        FROM share_links
        WHERE token = ?
        """,
        (token,),
    )
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, "公開リンクが無効です。"

    link_id = row["id"]
    one_time_code = row["one_time_code"]
    expires_at = row["expires_at"]
    remaining_uses = row["remaining_uses"]

    #期限チェック（UTCで比較）
    now_dt = datetime.now(timezone.utc)
    exp_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if now_dt > exp_dt:
        conn.close()
        return False, "公開リンクの有効期限が切れています。"

    #残り使用回数チェック
    if remaining_uses <= 0:
        conn.close()
        return False, "このワンタイムコードはすでに使用されています。"

    #コード一致チェック
    if code != one_time_code:
        conn.close()
        return False, "ワンタイムコードが正しくありません。"

    #ここまで来たらOK → 残り使用回数を1減らす
    cur.execute(
        "UPDATE share_links SET remaining_uses = remaining_uses - 1 WHERE id = ?",
        (link_id,),
    )
    conn.commit()
    conn.close()

    return True, ""
