// --- ラズパイ側コード ---


import { requestGPIOAccess } from "node-web-gpio";
import { exec } from 'child_process';
import fs from 'fs';
import path from 'path';
import sharp from 'sharp';
import { getAudioUrl } from 'google-tts-api';

// --- 【設定】サーバー・API関連 ---
const API_URL = "https://miseroom.pythonanywhere.com/api/upload_from_pi";
const USER_ID = process.env.USER_ID;

// --- 【設定】ファイル・ディレクトリ ---
const TEMP_FILE = "temp_capture.jpg";
const RESIZE_DIR = "resize";
const AUDIO_SAVE_FILE = "voice_msg.mp3";

// --- 【設定】スケジュール・センサー関連 ---
const HOUR_camera = 10;  // 朝のタスク時間
const HOUR_houkoku = 15; // 夕方のタスク時間

const TIME_checkPresence_mor = 5000;
const TIME_checkPresence_eve = 3000;

const DoMorningTask = true;
const DoEveningTask = true;

// --- グローバル変数 ---
let portRed, portYellow, portGreen, pirPort;
let isMorningTaskDone = false;
let isEveningTaskDone = false;
let currentDay = new Date().getDate();
let isProcessing = false;
let isPresent = true;

// ★追加: 朝のタスク用の経過日数カウンター
let morningDayCount = 0;

// 保存先フォルダ作成
if (!fs.existsSync(RESIZE_DIR)) {
    fs.mkdirSync(RESIZE_DIR, { recursive: true });
}

// ユーティリティ
const sleep = msec => new Promise(resolve => setTimeout(resolve, msec));

const runCommand = (cmd) => new Promise((resolve, reject) => {
    exec(cmd, (error, stdout, stderr) => {
        if (error) {
            console.error(`コマンドエラー: ${error.message}`);
            resolve(null);
        } else {
            resolve(stdout);
        }
    });
});

// ============================================================
//  ▼ アクション関数群 ▼
// ============================================================

async function saveTextToAudio(text) {
    if (!text) return;
    console.log(`💾 音声データを生成・保存中...: "${text}"`);

    try {
        const url = getAudioUrl(text, {
            lang: 'ja',
            slow: false,
            host: 'https://translate.google.com',
        });

        const res = await fetch(url);
        if (!res.ok) throw new Error(`音声ダウンロード失敗: ${res.status}`);

        const arrayBuffer = await res.arrayBuffer();
        const buffer = Buffer.from(arrayBuffer);

        fs.writeFileSync(AUDIO_SAVE_FILE, buffer);
        console.log(`✅ ファイルを保存しました: ${AUDIO_SAVE_FILE} (まだ再生しません)`);

    } catch (e) {
        console.error("❌ 音声保存失敗:", e);
    }
}

async function playSavedAudio() {
    console.log("🔊 保存された音声を再生します...");

    if (!fs.existsSync(AUDIO_SAVE_FILE)) {
        console.log("⚠️ 再生する音声ファイルがありません。(朝のタスクが実行されていない可能性があります)");
        return;
    }

    try {
        await runCommand(`mpg123 ${AUDIO_SAVE_FILE}`);
        console.log("✅ 再生終了");
    } catch (e) {
        console.error("❌ 再生失敗:", e);
    }
}

async function captureImage() {
    console.log("📸 カメラで撮影を開始します...");
    await runCommand(`rpicam-still -o ${TEMP_FILE} -t 1000 --width 1920 --height 1080`);
    console.log("✅ 撮影完了");
}

async function resizeImage() {
    console.log("🔄 画像をリサイズ中...");
    const now = new Date();
    const timestamp = now.toISOString().replace(/[-:T.]/g, '').slice(0, 14);
    const finalName = `capture_${timestamp}.jpg`;
    const finalPath = path.join(RESIZE_DIR, finalName);

    await sharp(TEMP_FILE)
        .resize({ width: 1024, height: 1024, fit: 'inside' })
        .jpeg({ quality: 80 })
        .toFile(finalPath);

    console.log(`✅ リサイズ完了: ${finalPath}`);
    if (fs.existsSync(TEMP_FILE)) fs.unlinkSync(TEMP_FILE);
    return finalPath;
}

async function uploadAndProcessResponse(filePath) {
    console.log(`📤 サーバーへ送信中...`);

    try {
        const fileBuffer = fs.readFileSync(filePath);
        const formData = new FormData();

        formData.append('image', new Blob([fileBuffer]), path.basename(filePath));
        formData.append('user_id', USER_ID);

        const now = new Date();
        const captured_at = now.getFullYear() + "-" +
            String(now.getMonth() + 1).padStart(2, '0') + "-" +
            String(now.getDate()).padStart(2, '0') + " " +
            String(now.getHours()).padStart(2, '0') + ":" +
            String(now.getMinutes()).padStart(2, '0') + ":" +
            String(now.getSeconds()).padStart(2, '0');
        formData.append('captured_at', captured_at);

        const response = await fetch(API_URL, {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`HTTPエラー: ${response.status}\n詳細: ${errorText}`);
        }

        const result = await response.json();
        console.log("📩 サーバー応答:", result);
        return result;

    } catch (err) {
        console.error("❌ 送信エラー:", err);
        return null;
    }
}

async function displayResult(result) {
    if (!result) return;

    console.log("\n--- 📊 判定結果 ---");

    const score = result.score;
    const due_at = result.due_at;
    const comment = result.comment || "";
    const advice = result.advice || "";

    const parts = [];

    if (score !== undefined && score !== null) {
        parts.push(`今回のスコアは ${score} 点です。`);
    }
    if (score !== undefined && score !== null && Number(score) < 50 && due_at) {
        parts.push(`掃除の期限は ${due_at} です。`);
    }

    // if (comment) parts.push(comment);
    // if (advice) parts.push(advice);

    const finalMessage = parts.join(" ");
    console.log("🗣️ [保存するテキスト]:", finalMessage);

    await saveTextToAudio(finalMessage);

    console.log("-------------------\n");
}

async function runMorningRoutine() {
    try {
        await captureImage();
        const filePath = await resizeImage();
        const result = await uploadAndProcessResponse(filePath);
        await displayResult(result);
        return true;
    } catch (e) {
        console.error("朝のタスクでエラー発生:", e);
        return false;
    }
}

// ============================================================
//  ▼ センサー・制御関数群 ▼
// ============================================================

async function checkPresence(durationMs) {
    console.log(`👀 センサー監視開始 (${durationMs / 1000}秒)...`);
    const interval = 500;
    const steps = durationMs / interval;
    for (let i = 0; i < steps; i++) {
        const val = await pirPort.read();
        if (val === 1) return true;
        await sleep(interval);
    }
    return false;
}

async function blinkOnce(red, yellow, green, millisecond) {
    if (!portRed) return;
    await portRed.write(red);
    await portYellow.write(yellow);
    await portGreen.write(green);
    if (millisecond) {
        await sleep(millisecond);
        await portRed.write(0);
        await portYellow.write(0);
        await portGreen.write(0);
        await sleep(millisecond);
    }
}

async function checkScheduleAndSensors() {
    const now = new Date();
    const hour = now.getHours();

    // 日付変更チェックとリセットロジック
    if (now.getDate() !== currentDay) {
        console.log("📅 日付が変わりました。");
        currentDay = now.getDate();

        // 1. 夕方のタスク: 毎日リセット
        isEveningTaskDone = false;
        console.log("   👉 夕方のタスクをリセットしました(毎日)。");

        // 2. 朝のタスク: 3日に1回リセット
        morningDayCount++;
        console.log(`   ⏳ 朝のタスク経過日数: ${morningDayCount}日目`);

        if (morningDayCount >= 3) {
            isMorningTaskDone = false;
            morningDayCount = 0;
            console.log("   👉 3日経過したため、朝のタスクをリセットしました。");
        } else {
            console.log("   👉 まだ3日経っていないため、朝のタスクは維持します。");
        }
    }

    if (isProcessing) return;

    // ----------------------------------------------------
    // 1. 朝のタスク
    // ----------------------------------------------------
    if ((hour === HOUR_camera && !isMorningTaskDone) || (DoMorningTask && !isMorningTaskDone)) {
        isProcessing = true;
        console.log("🌅 朝のタスクチェック中...");

        isPresent = await checkPresence(TIME_checkPresence_mor);

        if (isPresent === false) {
            console.log("👀 人がいないことを確認しました。撮影・報告・音声保存を開始します。");

            const success = await runMorningRoutine();

            if (success) {
                isMorningTaskDone = true;
                console.log("✅ 朝タスク完了 (音声保存済み)");
            }
        } else {
            console.log("👀 人がいるため、撮影を見送ります。");
        }
        isProcessing = false;
    }

    // ----------------------------------------------------
    // 2. 夕方のタスク
    // ----------------------------------------------------
    if ((hour === HOUR_houkoku && !isEveningTaskDone) || (DoEveningTask && !isEveningTaskDone)) {
        isProcessing = true;
        console.log("🌇 夕方のタスクチェック中...");

        isPresent = await checkPresence(TIME_checkPresence_eve);

        if (isPresent === true) {
            console.log("👀 人がいることを確認しました。");

            await playSavedAudio();

            isEveningTaskDone = true;
            console.log("✅ 夕方タスク完了");
        } else {
            console.log("👀 人がいないため、音声再生を見送ります。");
        }
        isProcessing = false;
    }
}

// ============================================================
//  ▼ メイン処理 ▼
// ============================================================
async function main() {
    console.log("🚀 システムを開始します...");

    const gpioAccess = await requestGPIOAccess();
    portRed = gpioAccess.ports.get(26);
    portYellow = gpioAccess.ports.get(19);
    portGreen = gpioAccess.ports.get(13);
    await portRed.export("out");
    await portYellow.export("out");
    await portGreen.export("out");

    pirPort = gpioAccess.ports.get(12);
    await pirPort.export("in");

    console.log("✅ センサー初期化完了。監視を開始します。");
    await blinkOnce(1, 1, 1, 1000);

    pirPort.onchange = async () => {
        const value = await pirPort.read();
        if (value === 1) {
            blinkOnce(1, 1, 1);
        } else {
            blinkOnce(0, 0, 0);
        }
    };

    setInterval(() => {
        checkScheduleAndSensors();
    }, 6000);
}

main();