# AutoLikeCmt - Auto Like & Comment TikTok

Tool tu dong like va comment video TikTok, chay hang loat nhieu tai khoan.

## Yeu cau he thong

- **macOS** (Apple Silicon hoac Intel)
- **Google Chrome** da cai dat
- **Ket noi internet**

## Cai dat & Chay (macOS)

### Cach 1: Dung DMG (khuyen dung)

1. Tai file `AutoLikeCmt.dmg` tu [Releases](../../releases)
2. Mo DMG, keo `AutoLikeCmt.app` vao **Applications**
3. Mo app lan dau:
   - Chuot phai vao app > **Open** > **Open** (cho phep chay)
   - Terminal tu dong mo va cai dat (5-10 phut)
   - App tu khoi dong sau khi cai xong
4. Lan sau: mo app tu Applications - chay luon

### Cach 2: Chay tu source code

```bash
# Clone repo
git clone https://github.com/Tranducvuwork/TikTokLoiBuff.git
cd TikTokLoiBuff

# Cai dat
cd AutoLikeCmt-v90-1000Live-a1b5068ae139-data
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt

# Chay
python3 run_macos.py
```

### Xoa quarantine (neu bi loi tren may moi)

```bash
xattr -cr ~/AutoLikeCmt/
```

## Nhap account

Mo Terminal va chay:

```bash
open ~/.auto-like-cmt/
```

Sua file `local_pool_data.json`:

```json
{
  "accounts": [
    {
      "id": "123456",
      "username": "tenuser",
      "login_user": "tenuser",
      "password": "matkhau",
      "email": "",
      "mail_pass": "",
      "totp_secret": "",
      "cookie": "cookie_string_day_du",
      "status": "live"
    }
  ]
}
```

## Build DMG

```bash
bash build_dmg.sh
```

DMG se duoc tao tai `AutoLikeCmt.dmg` voi giao dien keo tha vao Applications.

## Cau truc thu muc

```
TikTokLoiBuff/
├── AutoLikeCmt-v90-1000Live-a1b5068ae139-data/
│   ├── runtime/          # Core app code
│   │   ├── tiktok_reply.py    # Logic chinh
│   │   ├── client/            # Web UI (pywebview)
│   │   └── ...
│   ├── run_macos.py      # Entry point macOS
│   └── profile/          # Profile data
├── build_dmg.sh          # Script tao DMG
├── requirements.txt      # Python dependencies
├── HUONG-DAN.txt         # Huong dan su dung
└── README.md
```

## Luu y

- Lan dau chay tren may moi, macOS co the chan app. Vao **System Settings > Privacy & Security** de cho phep
- Du lieu luu tai: `~/.auto-like-cmt/`
- Can co Google Chrome de chay automation
