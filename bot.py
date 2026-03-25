import json
import os
import logging
import asyncio
import re
import httpx
from dotenv import load_dotenv

load_dotenv() # .env dosyasını yükle
import google.generativeai as genai
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ContextTypes, ConversationHandler, PollAnswerHandler, CallbackQueryHandler
)
from datetime import datetime
import pytz
from gtts import gTTS

# --- 1. YAPILANDIRMA ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DATA_FILE = "kpss_2026_data.json"
SINAV_TARIHI = datetime(2026, 7, 19, 10, 0, 0, tzinfo=pytz.timezone("Europe/Istanbul"))

# Startup Check
if not all([GEMINI_API_KEY, OPENROUTER_API_KEY, TELEGRAM_TOKEN]):
    print("❌ HATA: API Anahtarları Eksik!")
    logging.error("❌ HATA: GEMINI_API_KEY, OPENROUTER_API_KEY or TELEGRAM_TOKEN not found in environment!")
    if not TELEGRAM_TOKEN:
        print("Kritik Hata: TELEGRAM_TOKEN bulunamadı. Bot başlatılamıyor.")

if TELEGRAM_TOKEN:
    genai.configure(api_key=GEMINI_API_KEY)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO, force=True)

def mask_key(key): return f"{key[:6]}...{key[-4:]}" if key else "EKSİK"
print(f"✅ Bot Başlatıldı.")
print(f"🔑 Gemini Key: {mask_key(GEMINI_API_KEY)}")
print(f"🔑 OpenRouter Key: {mask_key(OPENROUTER_API_KEY)}")

# --- 2. DURUMLAR VE HAFIZA ---
SINAV, BRANS, HEDEF, NET, ZAYIF, SAAT = range(6)
ACTIVE_FREE_MODELS = [
    "google/gemini-2.0-flash-exp:free", 
    "google/gemini-flash-1.5-8b:free", 
    "meta-llama/llama-3.1-8b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/pixtral-12b:free",
    "google/gemma-2-9b-it:free",
    "qwen/qwen-2-7b-instruct:free",
    "microsoft/phi-3-medium-128k-instruct:free"
]
POLL_TO_USER = {} 
data_lock = asyncio.Lock()

PANEL_METNI = """
🛠 **KONTROL PANELİ (V31.0 NİHAİ)**

| Komut | Açıklama |
| :--- | :--- |
| `/quiz` | TÜM BRANŞLAR (T, M, Tar, C, V, G) |
| `/deneme` | Net Kaydı (Örn: /deneme 40 35) |
| `/durum` | Puan ve Kişiselleştirilmiş Yol Haritası |
| `/cevap` | Son Quiz Hatalarını Sesli Dinle |

📌 *Hata çözüldü, tüm sistemler mühendislik standartlarında aktif.*
"""

# --- 3. DİNAMİK MODEL KEŞFİ (404 FIX) ---
async def model_kesfi():
    global ACTIVE_FREE_MODELS
    url = "https://openrouter.ai/api/v1/models"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=15.0)
            if resp.status_code == 200:
                data = resp.json().get('data', [])
                discovered = [m['id'] for m in data if ':free' in m['id']]
                if discovered: ACTIVE_FREE_MODELS = discovered
    except Exception as e:
        print(f"❌ Model keşfi hatası: {e}")
    print(f"✅ Aktif ücretsiz modeller: {len(ACTIVE_FREE_MODELS)} adet bulundu.")

# --- 4. VERİ VE MESAJ YÖNETİMİ ---
AI_INSTRUCTIONS = {
    "info": "Kısa KPSS hap bilgisi. Bilgi ölçücü ve net.",
    "quiz": (
        "SEN BİR ÖSYM SORU YAZARISIN. Çeldiricileri çok güçlü, muhakeme gerektiren 5 adet soru hazırla. "
        "SADECE JSON dön. DİL TAMAMEN TÜRKÇE OLSUN. Başka dilden kelime kullanma. JSON formatı dışına çıkma. "
        "ÖNEMLİ: 'o' anahtarı MUTLAKA bir liste (Array) olmalı (['A', 'B', 'C']). "
        "Format: [{\"q\": \"Soru Metni\", \"o\": [\"Cevap A\", \"Cevap B\", \"Cevap C\", \"Cevap D\", \"Cevap E\"], "
        "\"a\": 0, \"s\": \"Konu\", \"e\": \"Çözüm Açıklaması\", \"cat\": \"Ders\"}]"
    ),
    "roadmap": "93 puan hedefli, mühendislik disiplinine uygun analitik strateji. Mühendislik terminolojisi kullan.",
    "lesson": "Konuyu ÖSYM can alıcı noktaları üzerinden derinlemesine anlat."
}

async def veri_yukle():
    async with data_lock:
        if not os.path.exists(DATA_FILE): return {}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return {}

async def veri_kaydet(data):
    async with data_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)

async def mesaj_parcali_gonder(update, context, text):
    if not text: return
    clean_text = re.sub(r'<think>.*?</think>', '💭 *Analiz:* ', text, flags=re.DOTALL).strip()
    clean_text = clean_text.replace("*", "").replace("#", "")
    MAX = 3800
    for i in range(0, len(clean_text), MAX):
        part = clean_text[i:i + MAX]
        if update.message: await update.message.reply_text(part)
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text=part)
        await asyncio.sleep(0.5)

# --- 5. ÜST DÜZEY AI MOTORU (HİBRİT) ---
async def openrouter_call(prompt, mode="info"):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "HTTP-Referer": "https://kpsskoc.com", "X-Title": "KPSS Master"}
    instruction = AI_INSTRUCTIONS.get(mode, mode)
    
    for model_id in ACTIVE_FREE_MODELS:
        payload = {
            "model": model_id, 
            "messages": [{"role": "user", "content": f"TALİMAT: {instruction}\nİSTEK: {prompt}"}],
            "max_tokens": 4000
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers, json=payload, timeout=60.0)
                if resp.status_code == 200:
                    content = resp.json()['choices'][0]['message']['content'].strip()
                    print(f"✅ OpenRouter ({model_id}) Başarılı")
                    return content
                else:
                    print(f"❌ OpenRouter Hata ({model_id}): {resp.status_code}")
                    logging.error(f"OpenRouter Error ({model_id}): {resp.status_code} - {resp.text}")
        except Exception as e:
            logging.error(f"OpenRouter Exception ({model_id}): {e}")
            continue
    return None

async def hybrid_engine(prompt, mode="info"):
    instruction = AI_INSTRUCTIONS.get(mode, mode)
    try:
        print(f"🚀 AI Denemesi: {mode} | Gemini-2.0-Flash")
        model = genai.GenerativeModel('gemini-2.0-flash')
        response = await model.generate_content_async(
            f"TALİMAT: {instruction}\nİSTEK: {prompt}",
            generation_config={"max_output_tokens": 4000}
        )
        if response and response.text:
            print("✅ Gemini Başarılı.")
            return response.text.strip()
        else:
            raise Exception("Gemini boş döndü.")
    except Exception as e:
        print(f"⚠️ Gemini Başarısız: {e}. OpenRouter'a geçiliyor...")
        return await openrouter_call(prompt, mode)

# --- 6. ONBOARDING (AWAIT HATASI ÇÖZÜLDÜ) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await veri_yukle()
    if str(update.message.from_user.id) in data:
        await update.message.reply_text(f"Selam! Tekrar aktifsin.\n{PANEL_METNI}")
        return ConversationHandler.END
    await update.message.reply_text("Merhaba! 93 Puan hedefli analitik KPSS Koçun hazır. Sınav türün?")
    return SINAV

async def sinav_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['tip'] = update.message.text
    await update.message.reply_text("Branşın nedir?")
    return BRANS

async def brans_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['brans'] = update.message.text
    await update.message.reply_text("Hedef puanın?")
    return HEDEF

async def hedef_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['hedef'] = update.message.text
    await update.message.reply_text("Şu anki netlerin?")
    return NET

async def net_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['net'] = update.message.text
    await update.message.reply_text("Zorlandığın dersler?")
    return ZAYIF

async def zayif_al(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['zayif'] = update.message.text
    await update.message.reply_text("Günde kaç saat çalışabilirsin?")
    return SAAT

async def onboard_bitir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = context.user_data
    status = await update.message.reply_text("⏳ Stratejin ÖSYM komisyonu modunda analiz ediliyor...")
    prompt = f"Branş: {u.get('brans')}, Hedef: {u.get('hedef')}, Zayıf: {u.get('zayif')}"
    roadmap = await hybrid_engine(prompt, mode="roadmap")
    data = await veri_yukle()
    user_id = str(update.message.from_user.id)
    data[user_id] = {
        "ad": update.message.from_user.first_name, 
        "brans": u.get('brans'), 
        "roadmap": roadmap, 
        "egitim": {}, 
        "denemeler": [],
        "stats": {"dogru": 0, "yanlis": 0, "hatali_konular": []}
    }
    await veri_kaydet(data)
    await status.delete()
    await update.message.reply_text("✅ **YOL HARİTAN HAZIR:**")
    await mesaj_parcali_gonder(update, context, roadmap)
    return ConversationHandler.END

# --- 7. QUIZ VE ANALİZ ---
async def quiz_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📚 Karma Deneme", callback_data="quiz_Karma")],
        [InlineKeyboardButton("🇹🇷 Türkçe", callback_data="quiz_Türkçe"), InlineKeyboardButton("🔢 Matematik", callback_data="quiz_Matematik")],
        [InlineKeyboardButton("📜 Tarih", callback_data="quiz_Tarih"), InlineKeyboardButton("🌍 Coğrafya", callback_data="quiz_Coğrafya")],
        [InlineKeyboardButton("⚖️ Vatandaşlık", callback_data="quiz_Vatandaşlık"), InlineKeyboardButton("📰 Güncel", callback_data="quiz_Güncel")]
    ]
    await update.message.reply_text("ÖSYM seviyesinde branş seç:", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("quiz_"):
        ders = query.data.split("_")[1]
        msg = await query.edit_message_text(f"⏳ **{ders}** için kaliteli sorular üretiliyor...")
        raw = await hybrid_engine(ders, mode="quiz")
        if not raw:
            await msg.edit_text("❌ AI şu an çok yoğun. Lütfen 30 saniye sonra tekrar deneyin.")
            return

        try:
            # JSON temizleme ve iyileştirme
            raw_clean = raw.replace("```json", "").replace("```", "").strip()
            
            # Kesilme onarımı: Eğer JSON liste kapanmamışsa, son tam objeden sonra kapat
            if raw_clean.startswith('[') and not raw_clean.endswith(']'):
                last_brace = raw_clean.rfind('}')
                if last_brace != -1:
                    raw_clean = raw_clean[:last_brace+1] + ']'
                    logging.info("⚠️ JSON kesilmiş bulundu, onarıldı.")

            json_match = re.search(r'\[.*\]', raw_clean, re.DOTALL)
            if not json_match:
                raise ValueError("JSON listesi ([...]) bulunamadı")
            
            processed_raw = json_match.group()
            
            # Kritik Hata Düzeltme: "o":"A","B" -> "o":["A","B"]
            # 'o'dan sonra [ gelmiyorsa, bir sonraki anahtar olan 'a'ya kadar olan kısmı köşeli paranteze al.
            processed_raw = re.sub(r'"o"\s*:\s*([^\[].*?)\s*,\s*"a"\s*:', r'"o": [\1], "a":', processed_raw, flags=re.DOTALL)
            
            # Başarılı parse sonrası mesajı sil (önce silme ki hata olursa kullanıcıyı bilgilendir)
            quiz_data = json.loads(processed_raw)
            
            for i, item in enumerate(quiz_data):
                # Seçeneklerin liste olduğunu doğrula ve her birini string'e çevir
                raw_options = item.get('o', [])
                if isinstance(raw_options, str):
                    raw_options = [opt.strip() for opt in raw_options.split(',')]
                
                # Seçeneklerin string olduğundan emin ol ve en az 2 seçenek olduğunu kontrol et
                options = [str(opt) for opt in raw_options]
                if len(options) < 2:
                    logging.warning(f"Soru {i+1} yetersiz seçenek nedeniyle atlandı.")
                    continue
                
                # Correct option id sınır kontrolü
                cor_id = int(item.get('a', 0))
                if cor_id >= len(options): cor_id = 0
                
                p = await context.bot.send_poll(
                    chat_id=query.message.chat_id, 
                    question=f"{i+1}. {item['q']}", 
                    options=options[:5], 
                    type='quiz', 
                    correct_option_id=cor_id,
                    is_anonymous=False, 
                    explanation=str(item.get('e', ''))[:200]
                )
                POLL_TO_USER[p.poll.id] = {
                    "user_id": query.from_user.id, 
                    "correct_id": int(item['a']), 
                    "subject": item['s'], 
                    "cat": item.get('cat', ders)
                }
                await asyncio.sleep(0.5)
            
            # Tüm sorular başarıyla gönderildiyse "üretiliyor" mesajını sil
            try: await msg.delete()
            except: pass
        except Exception as e:
            logging.error(f"Quiz Parse Error: {e}\nRaw Response: {raw[:500]}")
            try:
                await msg.edit_text(f"⚠️ Hata: {str(e)[:50]}... Lütfen tekrar deneyin.")
            except:
                await context.bot.send_message(chat_id=query.message.chat_id, text="⚠️ Soru üretiminde bir sorun oluştu.")

    elif query.data.startswith("exp_"):
        parts = query.data.split("|")
        konu = parts[1] if len(parts) > 1 else "Genel"
        wait = await query.message.reply_text(f"🎙 **{konu[:30]}** sesli analizi hazırlanıyor...")
        anlatim = await hybrid_engine(konu, mode="lesson")
        tts = gTTS(text=anlatim, lang='tr')
        f_path = f"lesson_{query.from_user.id}.mp3"
        tts.save(f_path)
        await wait.delete()
        with open(f_path, 'rb') as audio:
            await context.bot.send_voice(chat_id=query.message.chat_id, voice=audio, caption=konu)
        os.remove(f_path)

async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    if ans.poll_id in POLL_TO_USER:
        p = POLL_TO_USER[ans.poll_id]
        user_id = str(ans.user.id)
        data = await veri_yukle()
        if user_id not in data:
            data[user_id] = {"ad": ans.user.first_name, "stats": {"dogru": 0, "yanlis": 0, "hatali_konular": []}, "egitim": {}, "denemeler": []}
        if "stats" not in data[user_id]:
            data[user_id]["stats"] = {"dogru": 0, "yanlis": 0, "hatali_konular": []}
            
        if ans.option_ids[0] == p['correct_id']:
            data[user_id]["stats"]["dogru"] += 1
        else:
            data[user_id]["stats"]["yanlis"] += 1
            subj = str(p['subject']).replace('|', '')[:25]
            cat = str(p.get('cat', '')).replace('|', '')[:10]
            data[user_id]["stats"]["hatali_konular"].append(f"{subj}|{cat}")
        await veri_kaydet(data)

async def cevap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u_id = str(update.message.from_user.id)
    data = await veri_yukle()
    s = data.get(u_id, {}).get("stats")
    if not s or (s['dogru'] + s['yanlis'] == 0): return await update.message.reply_text("Henüz çözülmüş quiz veya sonuç yok.")
    
    hatali_list = list(set(s['hatali_konular']))[-8:] # Limit to 8 elements for inline keyboard
    btns = [[InlineKeyboardButton(f"🎙 {f.split('|')[0][:25]}", callback_data=f"exp_|{f}"[:64])] for f in hatali_list]
    await update.message.reply_text(f"Skor: {s['dogru']}D {s['yanlis']}Y.\nEksik Konularının Analizi:", reply_markup=InlineKeyboardMarkup(btns) if btns else None)

async def durum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = await veri_yukle()
    
    if user_id not in data:
        await update.message.reply_text("Henüz kaydın bulunmuyor. Başlamak için /start komutunu kullanabilirsin.")
        return

    u = data[user_id]
    stats = u.get("stats", {"dogru": 0, "yanlis": 0, "hatali_konular": []})
    dogru = stats.get("dogru", 0)
    yanlis = stats.get("yanlis", 0)
    toplam = dogru + yanlis
    net = dogru - (yanlis * 0.25)
    
    baslik = f"📊 **GÜNCEL DURUM: {u.get('ad', 'Öğrenci')}**\n\n"
    icerik = (
        f"✅ Doğru: {dogru}\n"
        f"❌ Yanlış: {yanlis}\n"
        f"📉 Toplam Net: {net:.2f}\n"
        f"📚 Çözülen Soru: {toplam}\n\n"
        f"🗺 **KİŞİSEL YOL HARİTAN:**\n"
    )
    
    await update.message.reply_text(baslik + icerik)
    await mesaj_parcali_gonder(update, context, u.get("roadmap", "Henüz yol haritası oluşturulmamış."))

async def deneme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    args = context.args
    
    if len(args) < 2:
        await update.message.reply_text("Lütfen doğru ve yanlış sayısını belirtin.\nÖrnek: `/deneme 40 35`", parse_mode='Markdown')
        return

    try:
        dogru = float(args[0])
        yanlis = float(args[1])
        net = dogru - (yanlis * 0.25)
        
        data = await veri_yukle()
        if user_id not in data:
            data[user_id] = {
                "ad": update.effective_user.first_name, 
                "stats": {"dogru": 0, "yanlis": 0, "hatali_konular": []}, 
                "egitim": {}, 
                "denemeler": []
            }
        
        entry = {
            "tarih": datetime.now(pytz.timezone("Europe/Istanbul")).strftime("%d.%m.%Y %H:%M"),
            "dogru": dogru,
            "yanlis": yanlis,
            "net": net
        }
        data[user_id].setdefault("denemeler", []).append(entry)
        await veri_kaydet(data)
        
        await update.message.reply_text(
            f"✅ **Deneme Kaydedildi!**\n\n📈 Net: **{net:.2f}**\n📅 Tarih: {entry['tarih']}\n\nBaşarılar dilerim, çalışmaya devam! 🔥", 
            parse_mode='Markdown'
        )
    except ValueError:
        await update.message.reply_text("Lütfen sayısal değerler girin (Örn: 40 35).")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 **Sistem Teşhis Modu Başlatıldı...**", parse_mode='Markdown')
    env_status = (
        f"✅ Gemini Key: {'Yüklü' if GEMINI_API_KEY else '❌ EKSİK'}\n"
        f"✅ OpenRouter Key: {'Yüklü' if OPENROUTER_API_KEY else '❌ EKSİK'}\n"
        f"✅ Telegram Token: {'Yüklü' if TELEGRAM_TOKEN else '❌ EKSİK'}\n"
    )
    await msg.edit_text(f"🔍 **Sistem Teşhis Modu**\n\n**Ortam Değişkenleri:**\n{env_status}", parse_mode='Markdown')
    
    # Test Gemini
    try:
        loop = asyncio.get_event_loop()
        test_gemini = await loop.run_in_executor(None, lambda: client_gemini.models.generate_content(
            model="gemini-1.5-flash", contents="Hi, just say 'Gemini OK'"
        ))
        gemini_res = f"✅ Gemini: {test_gemini.text.strip()}"
    except Exception as e:
        gemini_res = f"❌ Gemini Hatası: {str(e)[:100]}"
    
    # Test OpenRouter
    await msg.edit_text(f"🔍 **Sistem Teşhis Modu**\n\n**Ortam Değişkenleri:**\n{env_status}\n{gemini_res}\n⏳ OpenRouter test ediliyor...", parse_mode='Markdown')
    try:
        test_or = await openrouter_call("Hi, just say 'OR OK'", mode="info")
        or_res = f"✅ OpenRouter: {test_or if test_or else 'Dönüş Yok'}"
    except Exception as e:
        or_res = f"❌ OpenRouter Hatası: {str(e)[:100]}"
    
    final_text = (
        f"🔍 **Sistem Teşhis Sonuçları**\n\n"
        f"**Ortam Değişkenleri:**\n{env_status}\n"
        f"**Bağlantı Testleri:**\n"
        f"{gemini_res}\n"
        f"{or_res}\n\n"
        f"**Aktif Modeller:**\n`{ACTIVE_FREE_MODELS[:3]}...`\n\n"
        f"💡 *Not:* Eğer anahtarlar 'EKSİK' görünüyorsa Render Dashboard üzerinden eklemelisin."
    )
    logging.info(f"🔍 DEBUG SONUCU:\n{final_text}")
    await msg.edit_text(final_text, parse_mode='Markdown')

# --- 8. MAIN ---
import keep_alive

def main():
    keep_alive.keep_alive()
    asyncio.run(model_kesfi())
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SINAV: [MessageHandler(filters.TEXT & ~filters.COMMAND, sinav_al)],
            BRANS: [MessageHandler(filters.TEXT & ~filters.COMMAND, brans_al)],
            HEDEF: [MessageHandler(filters.TEXT & ~filters.COMMAND, hedef_al)],
            NET: [MessageHandler(filters.TEXT & ~filters.COMMAND, net_al)],
            ZAYIF: [MessageHandler(filters.TEXT & ~filters.COMMAND, zayif_al)],
            SAAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_bitir)],
        }, fallbacks=[]
    )
    
    app.add_handler(conv)
    app.add_handler(CommandHandler("quiz", quiz_menu))
    app.add_handler(CommandHandler("cevap", cevap))
    app.add_handler(CommandHandler("durum", durum))
    app.add_handler(CommandHandler("deneme", deneme))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(PollAnswerHandler(poll_handler))
    
    print("--- KPSS BOT V31.0 NİHAİ AKTİF (POLLING) ---")
    app.run_polling()

if __name__ == '__main__': main()