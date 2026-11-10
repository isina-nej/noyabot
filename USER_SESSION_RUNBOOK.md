# اجرای نویا روی اکانت شخصی تلگرام (Telethon)

این قابلیت به نسخه اصلی runbot دست نمی‌زند. اجرای runuser با Telegram API ID/HASH و فایل سشن اکانت شخصی انجام می‌شود؛ BOT_TOKEN همچنان فقط برای ربات اصلی است.

**هشدار امنیتی:** فایل noyabot.session پیش‌تر در مخزن عمومی قرار داشته است. حذف فایل از برنچ، نسخه تاریخی آن را پاک نمی‌کند. در Telegram > Settings > Devices نشست قدیمی را Terminate کن، سپس یک نشست تازه برای این سرویس بساز. session، api_hash، کد ورود و رمز دومرحله‌ای را هیچ‌وقت به Git یا چت نفرست.

## نصب و تنظیم

سرور باید Python 3.12، وابستگی‌های requirements.txt و ffmpeg (با libopus) داشته باشد. api_id و api_hash را برای اکانت خودت از https://my.telegram.org دریافت کن.

~~~bash
cd /opt/noyabot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo apt-get install -y ffmpeg
~~~

این مقادیر را داخل /opt/noyabot/.env بگذار؛ هر دو کلید باید خصوصی بمانند:

~~~dotenv
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=replace-me
NOYA_USER_SESSION=/var/lib/noya/noya-user
NOYA_USER_ALLOWED_CHAT_IDS=-1001234567890,123456789
NOYA_USER_PRIVATE_REPLIES=false
NOYA_USER_HISTORY_SCAN=200
NOYA_USER_HISTORY_LINES=30
NOYA_USER_AGENT_ENABLED=false
NOYA_API_URL=http://127.0.0.1:20128/v1/chat/completions
NOYA_API_KEY=replace-me
NOYA_STT_ENABLED=true
~~~

فهرست NOYA_USER_ALLOWED_CHAT_IDS **اجباری** است. برای گروه‌ها معمولاً آیدی با -100 شروع می‌شود؛ در پیوی آیدی عددی کاربر را قرار بده. پیام‌های خصوصی تنها وقتی پاسخ داده می‌شوند که آیدی مجاز باشد و NOYA_USER_PRIVATE_REPLIES=true تنظیم شده باشد.

ابتدا با همان کاربر غیر root که سرویس قرار است اجرا شود، از یک ترمینال امن لاگین کن:

~~~bash
id noya >/dev/null 2>&1 || sudo useradd --system --user-group --home /var/lib/noya --shell /usr/sbin/nologin noya
sudo install -d -m 700 -o noya -g noya /var/lib/noya
sudo chown noya:noya /opt/noyabot/.env
sudo chmod 600 /opt/noyabot/.env
sudo -u noya -H bash -c 'cd /opt/noyabot && .venv/bin/python manage.py runuser --login'
~~~

اگر از دیتابیس SQLite استفاده می‌کنی، یوزر noya باید روی فایل DB و پوشه آن حق نوشتن داشته باشد؛ در production با MySQL از مجوزهای DB_* فعلی پروژه استفاده کن. استفاده از یک فایل .env مشترک به معنی دسترسی یوزر سرویس به کلیدهای آن است؛ در محیط حساس متغیرهای موردنیاز را در فایل محیطی اختصاصی و محدود قرار بده. کد ورود تلگرام و رمز دومرحله‌ای صرفاً در ترمینال دریافت می‌شوند. پس از لاگین و ساخت فایل session، اجرای تعاملی را با Ctrl+C متوقف و سرویس را فعال کن:

~~~bash
sudo cp deploy/observability/noya-user.service.example /etc/systemd/system/noya-user.service
sudo systemctl daemon-reload
sudo systemctl enable --now noya-user
sudo journalctl -u noya-user -f
~~~

سرویس production بدون --login اجرا می‌شود و اگر نشست هنوز تأیید نشده باشد، با خطا متوقف می‌شود. فایل یکسان session را با دو فرآیند هم‌زمان اجرا نکن.

## ویژگی‌های قابل استفاده

- در گروه‌های مجاز فقط با «نویا»، Noya/Nuya، منشن مستقیم @username اکانت یا ریپلای به پیام اکانت پاسخ می‌دهد.
- در پیوی مجاز و با فعال شدن فلگ خصوصی، به پیام‌های متنی/رسانه‌ای جواب می‌دهد.
- از call_noya_api و run_ai_with_memory پروژه استفاده می‌کند؛ حافظه user-session از Bot API جدا می‌شود.
- حداکثر ۲۰۰ پیام قبل از درخواست از همان گروه خوانده می‌شود و فقط حداکثر ۳۰ پیام به‌عنوان زمینه غیرقابل‌اعتماد وارد مدل می‌شود.
- دستور /draw و «عکس بساز ...»، ریپلای «عکس» به متن، ساخت عکس و ویرایش عکس با دستور ریپلای.
- دستور /voice و «ویس ...»، ریپلای «ویس» به متن، TTS مشترک؛ با ffmpeg خروجی واقعی OGG/Opus voice note ارسال می‌شود.
- تبدیل ویس به متن، فریم ویدیو، خواندن سند، تصویر و استیکر با سرویس‌های موجود (در صورت تنظیم سرویس STT).
- در پردازش بیش از ۶۰ ثانیه پیام موقت ارسال و پس از پاسخ یا timeout حذف می‌شود.
- محدودسازی به آیدی‌های مجاز، نادیده گرفتن پیام‌های خود و دیگر ربات‌ها و کانال‌های پخش؛ جلوگیری از پاسخ‌های هم‌زمان متعدد در یک چت.

## محدودیت مهم

در این مرحله ابزارهای Agent در user-session به‌صورت پیش‌فرض خاموش‌اند، زیرا بخشی از ابزارهای مدیریت گروه به Bot API و مجوزهای ربات وابسته‌اند. NOYA_USER_AGENT_ENABLED=true را فقط پس از بررسی امنیت ابزارها روشن کن. عملیات مدیریت گروه، کیبوردهای ربات، و بعضی امکانات اختصاصی Bot API هنوز ۱۰۰٪ هم‌ارز نیستند. بقیه برنامه اصلی دست‌نخورده اجرا می‌شود.

## بررسی

~~~bash
.venv/bin/python manage.py check
.venv/bin/python manage.py test botapp.test_user_session
sudo systemctl status noya-user
sudo journalctl -u noya-user -n 100 --no-pager
~~~

اگر ویس به صورت فایل MP3 ارسال شد، ffmpeg/libopus را بررسی کن. اگر STT یا تولید رسانه جواب نمی‌دهد، تنظیمات NOYA_STT_* و NOYA_API_* سرویس‌های اصلی را چک کن؛ رمز و سشن را در لاگ‌ها چاپ نکن.
