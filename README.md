# TinkeraRobot

ربات مدیریت گروه تلگرام با Django و aiogram؛ شامل moderation قابل تنظیم برای هر گروه، سهمیه هوش مصنوعی و پنل Django.

قابلیت‌های مدیریت شامل ضدفلود و تشخیص تکرار پیام، ضدلینک با دامنه‌های مجاز، ضدفوروارد، فیلتر کلمات، اخطار با انقضا و mute خودکار، mute/ban/unban دستی، قوانین، خوش‌آمد/خداحافظی، لاگ اقدامات و retention جداگانه برای هر گروه است.

نکته: ربات باید در گروه دسترسی‌های `Delete messages` و `Restrict members` داشته باشد. برای دریافت رویداد ورود/خروج اعضا، `ChatMemberUpdated` نیز باید در تنظیمات polling دریافت شود.

دستورات اصلی داخل گروه:

`/rules` نمایش قوانین، `/setrules متن` تنظیم قوانین، `/warn` در پاسخ به پیام کاربر، `/warns` نمایش اخطارها، `/mute`، `/unmute`، `/ban`، `/unban`، `/setwelcome متن` تنظیم خوش‌آمد، `/filter add|del کلمه` مدیریت کلمات ممنوع، `/allowdomain add|del example.com` مدیریت دامنه‌های مجاز، `/id` نمایش شناسه، و `/prompt سؤال` برای استفاده از AI.

فرمان‌های مجازات بدون اسلش هم کار می‌کنند. در پاسخ به پیام کاربر می‌توان نوشت: `warn`، `اخطار`، `هشدار`، `mute`، `میوت`، `سکوت`، `unmute`، `آنمیوت`، `ban`، `بن`، `مسدود`، `unban`، `آنبن`، `warns` یا `اخطارها`. ادمین‌ها و خود ربات در برابر warn، mute و ban محافظت می‌شوند.

## عملیات دائمی و زمان‌بندی‌شده

دستورهای `mute` و `ban` چهار حالت دارند. بدون عدد، عملیات فوری و دائمی است. با یک عدد، فوری و زمان‌دار است. با دو عدد، عدد اول تأخیر شروع و عدد دوم مدت عملیات است. واژه `دائمی` به‌جای مدت یعنی عملیات بعد از تأخیر شروع شود و پایان خودکار نداشته باشد. همه اعداد بر حسب دقیقه هستند.

```text
mute                    # سکوت فوری و دائمی
mute 30 spam            # سکوت فوری برای ۳۰ دقیقه
mute 60 دائمی spam      # شروع سکوت دائمی بعد از ۶۰ دقیقه
mute 60 30 spam         # شروع بعد از ۶۰ دقیقه، آزادی بعد از ۳۰ دقیقه
ban                     # بن فوری و دائمی
ban 30 abuse            # بن فوری برای ۳۰ دقیقه
ban 60 دائمی abuse      # بن دائمی با یک ساعت تأخیر
ban 60 30 abuse         # یک ساعت بعد بن؛ ۳۰ دقیقه بعد آزادسازی
```

قفل گروه هدف کاربر ندارد:

```text
/lock                   # قفل فوری و دائمی
/lock 30                # قفل فوری برای ۳۰ دقیقه
/lock 60 دائمی          # قفل دائمی با یک ساعت تأخیر
/lock 60 30             # یک ساعت بعد قفل؛ ۳۰ دقیقه بعد باز
/unlock                 # باز کردن فوری
```

معادل بدون اسلش فارسی `قفل`، `قفل‌گروه`، `باز`، `بازکردن` و `رفع‌قفل` نیز پشتیبانی می‌شود. باز کردن گروه، permissionهای قبل از قفل را بازیابی می‌کند.

سیاست سقف اخطار فقط یک مجازات از نوع `mute` یا `ban` دارد و سه مقدار مستقل می‌گیرد: `max_warnings_action`، `max_warnings_action_delay_minutes` و `max_warnings_action_duration_minutes`. مقدار duration خالی یعنی دائمی. این تنظیمات از پنل Django یا API قابل تغییرند.

## Backend و API

منطق زمان‌بندی در `botapp/moderation.py` مستقل از تلگرام است. اتصال Telegram در `botapp/telegram_gateway.py` و `botapp/telegram_moderation.py` قرار دارد؛ سایت آینده می‌تواند همان backend را از API استفاده کند.

مسیرهای API:

```text
GET|POST /api/groups/<chat_id>/settings/
POST     /api/moderation/actions/
GET      /api/moderation/actions/<id>/
POST     /api/moderation/actions/<id>/cancel/
```

احراز هویت با session کاربر staff و CSRF یا هدر `X-API-Key` انجام می‌شود. کلید API به‌صورت hash ذخیره می‌شود و متن آن فقط هنگام ساخت نمایش داده می‌شود:

```bash
.venv/bin/python manage.py create_api_key dashboard --username admin
```

ایجاد action از API نمونه:

```json
{
  "chat_id": -100123456,
  "action": "ban",
  "target_user_id": 42,
  "delay_minutes": 60,
  "duration_minutes": 30,
  "reason": "spam"
}
```

هدر `Idempotency-Key` از ثبت تکراری یک درخواست جلوگیری می‌کند. actionهای معتبر: `lock`، `unlock`، `mute`، `unmute`، `ban` و `unban`.

متن‌های قابل تنظیم در `GroupSettings.message_templates` ذخیره می‌شوند. کلیدهای فعلی: `warn`، `warning_ceiling`، `lock`، `unlock`، `mute`، `unmute`، `ban`، `unban`، `scheduled` و `admin_protected`. placeholderها بسته به پیام شامل `{target}`، `{count}`، `{max_warnings}`، `{action}` و `{execute_at}` هستند.

## پیام خوش‌آمد

قالب پیش‌فرض:

```text
سلام #name عزیز به گروه #title خوش آمدی 🌷 ✅ ساعت: ( #time ) ✅ تاریخ: ( #date )
```

قالب را می‌توان با `/setwelcome متن` تغییر داد. placeholderها:

```text
#name    نام قابل کلیک کاربر
#title   نام گروه
#time    ساعت محلی دقیق
#date    تاریخ میلادی همان روز
#datesh  تاریخ شمسی همان روز
```

## قفل روزانه در ساعت مشخص

برنامه‌های روزانه طبق `TIME_ZONE` پروژه اجرا می‌شوند و به worker نیاز دارند:

```text
/lockat 23:30       # هر روز ساعت ۲۳:۳۰ گروه قفل شود
/unlockat 08:00     # هر روز ساعت ۰۸:۰۰ گروه باز شود
/lockat off         # حذف برنامه قفل
/unlockat off       # حذف برنامه بازکردن
```

## حذف پیام

```text
حذف                # در پاسخ به یک پیام؛ پیام هدف و فرمان حذف می‌شوند
delete / del        # معادل انگلیسی
حذف 100            # حذف ۱۰۰ پیام قبل به‌همراه فرمان؛ سقف امن ۱۰۰۰
حذف تا اینجا       # در پاسخ به پیام مقصد؛ از آن پیام تا فرمان حذف می‌شود
سکوت حذف           # سکوت کاربر و حذف پیام هدف، فرمان و پاسخ ربات
بن حذف             # بن کاربر و حذف پیام‌ها
اخطار حذف          # اخطار و حذف پیام‌ها
```

محدودیت Telegram همچنان اعمال می‌شود: پیام‌های بسیار قدیمی یا برخی پیام‌های سرویس ممکن است قابل حذف نباشند. ربات باید دسترسی `Delete messages` داشته باشد.

worker عملیات موعددار باید کنار bot اجرا شود:

```bash
.venv/bin/python manage.py run_moderation_worker
```

برای اجرای یک batch و خروج:

```bash
.venv/bin/python manage.py run_moderation_worker --once
```


با ارسال واژه «پنل» توسط ادمین، پنل تنظیمات درون گروه باز می‌شود.

برای پاک‌سازی لاگ‌های قدیمی طبق retention هر گروه:

```bash
.venv/bin/python manage.py purge_moderation_logs
```

برای اجرای دوره‌ای این دستور از cron یا systemd timer استفاده کنید.

---

## راه‌اندازی

Python 3.11 یا جدیدتر لازم است.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# مقادیر .env را تنظیم کنید.
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runbot
```

برای اجرای وب‌اپ مدیریتی و health check:

```bash
.venv/bin/python manage.py runserver
```

مسیر سلامت سرویس `GET /health/` است و پنل Django در `/admin/` قرار دارد.

## بررسی پروژه

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

## امنیت

فایل `.env`، دیتابیس، لاگ و PID در مخزن نگهداری نمی‌شوند. توکن‌های افشاشده را در BotFather لغو و جایگزین کنید. در محیط production باید `DEBUG=false` و `DJANGO_SECRET_KEY` و `ALLOWED_HOSTS` معتبر تنظیم شوند.
# TinkeraRobot

## ۶. راهنمای هوش مصنوعی و System Prompt

پرامپت سیستمی اصلی شخصیت «نویا» در مسیر زیر قرار دارد:
`botapp/ai/prompts.py`

- **نسخه پرامپت**: `NOYA_SYSTEM_PROMPT_VERSION = "v7"` — برای صورت‌حساب ابزارهای تحقیق جدید، متن پرامپت را در همان فایل ببینید.
- **نحوه اتصال:** در فایل `botapp/services.py` از تابع `build_ai_messages` استفاده شده تا پرامپت دقیقاً در نقش `system` و قبل از متون کاربر و Context حافظه به `Payload` اضافه‌شود.
- **هویت سازنده:** `NOYA_CREATOR_IDS` (مثلاً `1399836576`) + `NOYA_CREATOR_NAME` / `NOYA_CREATOR_USERNAME`. وقتی کسی می‌پرسد سازنده‌اش کیست، باید با منشن HTML (`tg://user?id=…`) به همان نفر اشاره کند. بلوک `[SPEAKER]` نقش `creator|user` را مشخص می‌کند.
- **چت پیوی:** هر متن غیر‌دستوری در گفت‌وگوی خصوصی با نویا جواب داده می‌شود (نیازی به `/prompt` نیست).
- **چت گروه:** خطاب با `نویا …` / `@bot` / ریپلای به پیام ربات برای همهٔ اعضا کار می‌کند و نیازی نیست کاربر ادمین گروه باشد. اگر ربات ادمین گروه نباشد، تلگرام فقط منشن/`reply`/دستور را می‌رساند مگر Group Privacy در BotFather خاموش شود.
- **جواب به ربات‌های دیگر:** تلگرام به‌صورت پیش‌فرض پیام ربات‌ها را به نویا تحویل نمی‌دهد. در @BotFather برای نویا (و ترجیحاً ربات مقابل) گزینهٔ **Bot-to-Bot Communication Mode** را روشن کنید؛ بعد از آن ریپلای به پیام نویا یا تگ/`/command@bot` از سمت ربات‌ها هم جواب داده می‌شود (با محدودیت ضدحلقه در کد).
- **ویژگی‌ها:**
  - لحن انسانی‌تر: خودافشایی ربات‌گونه و پایان‌های «کمک دیگه‌ای؟» ممنوع است.
  - در یک Request فقط یک بار System Prompt ارسال می‌شود.
  - متن Prompt در دیتابیس (حافظه)، لاگ‌های عمومی یا متن پیام‌های فورواردشده ذخیره نمی‌شود.
  - با استفاده از فلگ متغیرمحیطی `NOYA_SYSTEM_PROMPT_ENABLED=false` می‌توان در زمان دیباگ یا رول‌بک آن را غیرفعال کرد.

## ۷. دستیار مدیریتی هوشمند (Agent)

ادمین می‌تواند با زبان طبیعی فارسی به ربات دستور مدیریتی بدهد. این قابلیت به‌صورت افزودنی روی معماری فعلی ساخته شده و همه‌ی فرمان‌ها و رفتارهای قبلی دست‌نخورده‌اند.

### تریگرها

```text
/agent <دستور>
/adminai <دستور>
نویا مدیر، <دستور>     ← همیشه به Agent می‌رود (Parser قطعی و سپس AI)
نویا، <دستور مدیریتی>  ← فقط اگر Parser قطعی Intent را تشخیص دهد
```

تفاوت دو تریگر متنی:

- **«نویا مدیر، …»** (فقط ادمین): همیشه به Agent مسیر داده می‌شود؛ ابتدا Parser قطعی و اگر تشخیص نداد، AI Structured Parser اجرا می‌شود. پس دستورات پیچیده‌ی نیازمند AI از دست نمی‌روند.
- **«نویا، …»** (فقط ادمین): فقط زمانی به Agent می‌رود که Parser قطعی یک Intent مطمئن تشخیص دهد؛ در غیر این‌صورت پیام به گفت‌وگوی عادی نویا fall-through می‌کند (رفتار قبلی حفظ شده است).

در هر دو حالت کاربران عادی هرگز نمی‌توانند این تریگرها را فعال کنند، عملیات ناشناخته/کم‌اطمینان اجرا نمی‌شود، و پیام Replyشده فقط به‌عنوان داده‌ی غیرقابل اعتماد وارد Context می‌شود. فرمان‌های قطعی موجود (mute، ban، حذف، قفل و…) و فرمان‌های بدون اسلش و `/prompt` بدون تغییر کار می‌کنند.

### مدیریت و تحلیل گروه/کانال (از داخل چت یا پیوی)

- **داخل گروه:** مثل قبل دستور بدهید (`/agent تحلیل کن`, `/agent آمار هفته`).
- **از پیوی ربات:** گروه/کانال هدف را در ابتدای دستور بنویسید یا یک پیام از آن چت را فوروارد کنید:

```text
/agent @mychannel آمار امروز
/agent @mychannel تحلیل کن
/agent -100123456789 لیست ادمین‌ها
/agent @mychannel یک پست با متن «سلام» بفرست
```

ربات باید در کانال/گروه هدف **عضو** باشد و برای عملیات نوشتن، **ادمین با دسترسی لازم** باشد. عملیات حساس همچنان تأیید شیشه‌ای می‌خواهند؛ دکمه‌های تأیید در همان چتی که دستور را زده‌اید نشان داده می‌شوند (حتی اگر هدف یک کانال دیگر باشد).

ابزارهای ویژه کانال: `channel.get_info`، `channel.get_subscriber_count`، `channel.get_admins`، `channel.post_text`، `channel.delete_post`، `channel.pin_post`.  
ابزار تحلیل: `analytics.generate_briefing` (آمار واقعی + خلاصه AI در صورت در دسترس بودن `NOYA_API_KEY`).

بررسی مرحله‌ای (ReAct harness — چند ابزار خواندنی + در صورت نیاز اجرای پایتون محدود روی دادهٔ جمع‌شده):

```text
/agent بررسی کامل کن
/agent تحلیل عمیق گروه
/agent مرحله به مرحله بررسی کن
```

ابزار: `harness.investigate`. فقط ابزارهای کم‌ریسک/خواندنی به‌صورت خودکار اجرا می‌شوند؛ عملیات نوشتنی همچنان مسیر تأیید جدا دارند. با `AGENT_HARNESS_AI_ENABLED=false` یک پاس قطعی از ابزارهای آماری اجرا می‌شود (بدون برنامه‌ریز LLM).

شمارش پیام (همیشه روشن، بدون نیاز به آرشیو کامل متن):

```text
/agent امروز چندتا پیام داد؟
/agent آمار پیام هفته
/agent فعال‌ترین‌های امروز
```

ربات فقط پیام‌هایی را می‌شمارد که خودش از تلگرام دریافت کند (از لحظه فعال‌سازی؛ Bot API تاریخچه قدیمی را برنمی‌گرداند). ریپلای ادمین به پیام ربات با سؤال‌های آماری/مدیریتی هم به Agent می‌رود، نه به چت عمومی نویا.

### جریان اجرا

`Trigger → Admin Verification → JEV-first 4-way Route (chat/admin/harness/media) → Parser Validation → (AI Structured Parser) → Schema Validation → Registry Lookup → App Permission → Bot Capability → Target Resolution → Risk → Confirmation → Existing-Service Execution → Audit → Persian Response`

- ابتدا مدل مسیر را انتخاب می‌کند (JEV SystemOne چهارمسیره، سپس JSON ساختاریافته 9Router)؛ Parser قطعی فقط اعتبارسنجی intent و جلوگیری از اجرای ناامن است، نه انتخاب‌کننده اصلی مسیر. اجرای ابزار فقط با confidence ≥ 0.80 مجاز است، وگرنه چت/شفاف‌سازی.
- مدل AI فقط `RequestClassification` (route/intent/confidence/target_source/reason) و سپس `AgentDecision` معتبر و allowlist‌شده تولید می‌کند؛ هرگز مستقیماً عملیات تلگرام یا دیتابیس انجام نمی‌دهد و هرگز `user_id`/`message_id`/`chat_id` نمی‌سازد.
- عملیات state‌دار مدیریتی از همان Pipeline موجود (`queue_or_execute` → `ModerationAction`) استفاده می‌کنند.
- کاربر هدف فقط از Reply تعیین می‌شود؛ مدل اجازه‌ی ساختن `user_id` را ندارد.
- محتوای Reply/نقل‌قول به‌عنوان `<untrusted_message>` (داده، نه دستور) به مدل داده می‌شود.

### تأیید عملیات حساس

عملیات پرخطر (ban، mute، قفل، حذف، تغییر تنظیمات امنیتی) پیش از اجرا یک پیام تأیید با دکمه‌های «✅ تأیید / ❌ لغو» نمایش می‌دهند. فقط درخواست‌دهنده، در همان گروه، و تا قبل از انقضا (پیش‌فرض ۱۲۰ ثانیه) می‌تواند تأیید کند. Tool Registry منبع نهایی سطح ریسک است؛ اگر مدل ریسک کمتری اعلام کند، مقدار Registry اعمال می‌شود.

رفتار دقیق Token تأیید:

- یک Token تصادفی و غیرقابل حدس با `secrets.token_urlsafe` (بیش از ۱۲۸ بیت entropy) تولید می‌شود.
- Token خام فقط داخل `callback_data` دکمه‌های Inline قرار می‌گیرد (`agent_confirm:<token>` / `agent_cancel:<token>`).
- در دیتابیس فقط SHA-256 Hash توکن ذخیره می‌شود (`token_hash`)، نه خودِ توکن.
- هنگام دریافت Callback، توکن Hash شده و رکورد با Hash پیدا می‌شود؛ توکن خام دور ریخته می‌شود.
- Token خام هرگز در Log، Audit Log یا مدل دیتابیس ذخیره نمی‌شود.
- طول `callback_data` همیشه زیر محدودیت ۶۴ بایتی تلگرام می‌ماند (پیشوند ≤۱۴ بایت + توکن ۳۲ کاراکتری).

### آرشیو پیام (اختیاری)

با `MESSAGE_ARCHIVE_ENABLED=true` ربات پیام‌های دریافتی گروه را آرشیو می‌کند و حذف‌هایی را که خودش انجام می‌دهد ثبت می‌کند. مطابق محدودیت واقعی Telegram، حذف دستی کاربران در گروه‌های عادی برای ربات قابل مشاهده نیست و ربات چنین ادعایی نمی‌کند. پاک‌سازی دوره‌ای (حتماً با cron / systemd timer زمان‌بندی کنید؛ در خودِ ربات scheduler داخلی برای این دستور نیست):

```bash
.venv/bin/python manage.py purge_message_snapshots
# مثال cron روزانه ساعت ۰۳:۱۵:
# 15 3 * * * cd /path/to/TinkeraRobot && .venv/bin/python manage.py purge_message_snapshots
```

### متغیرهای محیطی

```text
AGENT_ENABLED=true
AGENT_AI_ENABLED=true
AGENT_MODEL=
AGENT_CONFIRMATION_TTL_SECONDS=120
AGENT_MIN_CONFIDENCE=0.80
AGENT_MAX_COMMAND_LENGTH=2000
AGENT_HARNESS_ENABLED=true
AGENT_HARNESS_AI_ENABLED=true
AGENT_HARNESS_MAX_STEPS=5
MESSAGE_ARCHIVE_ENABLED=false
MESSAGE_ARCHIVE_RETENTION_DAYS=30
```

### تحقیق چندمنبعی وب + X + یوتیوب (فقط خواندنی)

لایه تحقیق در `botapp/research/` قرار دارد (providerهای `web` / `x` / `youtube` →‏ normalize → dedupe → rank → evidence با مرز `<external_source platform="...">`). همه خروجی‌ها داده غیرمعتمد‌اند و هرگز دستور سیستمی نیستند؛ credentialها (توکن X، کلید YouTube، کوکی bridge) هرگز وارد prompt، tool result، log یا audit نمی‌شوند.

ابزارهای چت (read-only — هیچ write/post/like/follow/comment فعال نیست):

```text
x.search_posts / x.get_post / x.get_thread / x.get_user / x.get_user_posts / x.get_replies
youtube.search / youtube.get_video / youtube.get_channel / youtube.get_comments / youtube.get_playlist / youtube.get_transcript
research.social_search   (تجمیع وب+X+یوتیوب؛ partial-failure کل تحقیق را fail نمی‌کند)
```

- **ترتیب X:** ابتدا Official X API v2 (با `X_BEARER_TOKEN`)؛ bridge محلی `x-bridge` (روی `127.0.0.1` + توکن داخلی `X_BRIDGE_INTERNAL_TOKEN`) فقط وقتی استفاده می‌شود که `X_SESSION_ENABLED=true` باشد.
- **YouTube:** جستجو/ویدیو/کامنت/پلی‌لیست با `YOUTUBE_API_KEY` (Data API v3). `youtube.get_transcript` هرگز transcript حدس نمی‌زند؛ اگر caption عمومی در دسترس نباشد `{available: false, reason}` برمی‌گرداند.
- **Feature flags:** `X_RESEARCH_ENABLED` و `YOUTUBE_RESEARCH_ENABLED` (هر دو پیش‌فرض `false`). وقتی provider خاموش/بدون credential است، ابزار پیام «فعال نیست» برمی‌گرداند. جزئیات کامل متغیرها در `.env.example` (فقط placeholder، بدون credential واقعی).
- **سقف‌ها:** هر provider حداکثر نتایج/صفحه، timeout حدود ۱۵ ثانیه، کل تحقیق ۲۵ ثانیه؛ حلقه چت حداکثر ۳ وب‌سرچ + ۶ fetch + ۴ تحقیق X/YouTube/اجتماعی در هر مکالمه انجام می‌دهد.

مدل‌های جدید `AgentConfirmation`، `AgentAuditLog` و `MessageSnapshot` در پنل Django قابل مشاهده‌اند. برای Rollback بدون تغییر کد کافی است `AGENT_ENABLED=false` تنظیم شود.

### بازیابی عملیات‌های گیرکرده

اگر یک Process پس از Claim‌شدن تأیید ولی پیش از پایان اجرا از کار بیفتد، رکورد ممکن است در وضعیت `executing` بماند. برای گزارش/بستن این رکوردها بدون اجرای مجدد عملیات خطرناک:

```bash
.venv/bin/python manage.py agent_confirmations_recover            # فقط گزارش
.venv/bin/python manage.py agent_confirmations_recover --fail      # علامت‌گذاری به‌عنوان failed (بدون اجرای مجدد)
```

### تست زنده با ربات مستقل

هرگز از توکن ربات production برای Polling استفاده نکنید. یک ربات آزمایشی جدا در BotFather بسازید و متغیرهای `TEST_BOT_TOKEN` (و اختیاری `TEST_GROUP_ID`، `TEST_ADMIN_USER_ID`) را تنظیم کنید، سپس:

```bash
.venv/bin/python manage.py run_testbot
```

این دستور فقط با `TEST_BOT_TOKEN` اجرا می‌شود و هرگز به `BOT_TOKEN` production بازنمی‌گردد؛ اگر متغیر تنظیم نشده باشد با خطای واضح متوقف می‌شود.

### تست روی دیتابیس Production-like (MySQL)

رفتار Lock هم‌زمان (`select_for_update`) در SQLite قابل اثبات نیست؛ تستِ `ConcurrentConfirmationTests` روی SQLite به‌صورت خودکار Skip می‌شود و فقط روی MySQL/Postgres اجرا می‌شود. برای اجرای کامل روی MySQL:

```bash
DB_ENGINE=django.db.backends.mysql DB_NAME=group_bot DB_USER=group_bot \
  DB_PASSWORD=... DB_HOST=127.0.0.1 DB_PORT=3306 \
  .venv/bin/python manage.py test
```

در CI پیش‌فرض (SQLite) این تست Skip می‌شود؛ برای پوشش کامل باید یک سرویس MySQL در CI اضافه شود.

### هشدار امنیتی: توکن افشاشده

اگر توکن ربات production در جایی مثل Chat، Log، Screenshot، Artifact یا محیط غیرمطمئن قرار گرفت، باید فوراً چرخانده (rotate) شود:

1. در BotFather دستور `/revoke` را برای همان ربات اجرا کنید تا توکن قدیمی باطل شود.
2. توکن جدید را فقط در یک Secret Manager (مثل بخش Secrets) قرار دهید، نه در کد/Chat/Log.
3. سرویس production را با توکن جدید Restart کنید.
4. توکن قدیمی را از تمام محیط‌های توسعه/CI حذف کنید.
5. Logها و Artifactها را برای نشت احتمالی توکن بررسی کنید.

هیچ توکن یا Secret نباید در Git، Log، Screenshot یا Artifact ذخیره شود.

### Observability و عیب‌یابی راه دور

پکیج \`botapp/observability/\` لاگ JSON محدود و redact‌شده، correlation/trace id، متریک‌های کم‌کاردینالیتی و exporter اختیاری OpenTelemetry را فراهم می‌کند. این لایه fail-open است؛ نبودن collector نباید پردازش تلگرام را متوقف کند. API عیب‌یابی فقط زیر مسیر خصوصی \`/api/internal/ops/v1/\` و با همان \`X-API-Key\` موجود فعال است:

\`\`\`text
GET  /health/ ، /runtime/ ، /logs/ ، /metrics/summary/ ، /traces/<id>/
GET  /tests/ و /diagnostics/
POST /diagnostics/ با بدنهٔ {"test_id":"process"}
\`\`\`

اجرای تشخیصی فقط از تست‌های allowlist‌شدهٔ read-only (\`process\` و \`database\`) استفاده می‌کند و shell، مسیر فایل یا کد دلخواه را اجرا نمی‌کند. برای دسترسی محلی از تونل SSH یا شبکهٔ خصوصی استفاده کنید:

\`\`\`bash
NOYAOPS_URL=http://127.0.0.1:8000/api/internal/ops/v1 \\
NOYAOPS_API_KEY='...' .venv/bin/python -m noyaops doctor
NOYAOPS_API_KEY='...' .venv/bin/python -m noyaops logs --level error
\`\`\`

برای خروجی ماشینی، \`--json\` را اضافه کنید؛ \`logs --follow\` نیز با cursor محدود به‌صورت polling کار می‌کند. تنظیمات CLI از \`~/.config/noyaops/config.toml\` (با مجوز ۶۰۰) خوانده می‌شود و متغیرهای \`NOYAOPS_*\` روی آن اولویت دارند. گزینه‌های \`status\`، \`health\`، \`runtime\`، \`metrics\`، \`trace\`، \`tests list/run/result\` و \`doctor\` در دسترس هستند؛ درخواست‌های خواندنی retry محدود و شناسهٔ \`X-Request-ID\` دارند.

پروفایل سبک در خود Django و با ring محدود کار می‌کند. پروفایل کامل نمونهٔ Grafana Alloy/OpenObserve، فایل داشبورد و واحد systemd را در \`deploy/observability/\` دارد؛ با \`docker compose --profile full\` اجرا می‌شود و پورت‌ها را روی localhost نگه می‌دارد. قبل از استفادهٔ production نسخهٔ image، منابع، retention و Secretها را بازبینی کنید.

