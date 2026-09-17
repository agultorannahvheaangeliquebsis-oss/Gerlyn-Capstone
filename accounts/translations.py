"""Lightweight site translation table.

Not Django's gettext machinery ({% trans %} + .po/.mo) — this machine (and
possibly the deploy target) doesn't have GNU gettext installed, and
`compilemessages` hard-fails without it. Two languages, no pluralization
needs, so a plain dict keyed by the English source string is simpler and
has zero external dependencies.

English is always the fallback: any string not present in a language's
dict (or an unsupported language code) just renders as the English text
passed to {% t %}, so English is never actually "missing".
"""

TRANSLATIONS = {
    'fil': {
        # ── Header / nav ──────────────────────────────────────────────
        'Home': 'Home',
        'Products': 'Mga Produkto',
        'Cart': 'Cart',
        'Log in': 'Mag-log In',
        'Log In': 'Mag-log In',
        'log in': 'mag-log in',
        'My Orders': 'Aking mga Order',
        'Settings': 'Mga Setting',
        'Logout': 'Mag-log Out',
        'Search products, brands…': 'Maghanap ng produkto, brand…',
        'Search': 'Maghanap',
        'Track Order': 'Subaybayan ang Order',
        'Free delivery within Baliuag on orders over ₱500': 'Libreng delivery sa loob ng Baliuag para sa orders na ₱500 pataas',
        '© 2026 Gerlyn Variety Store · Baliuag, Bulacan · All rights reserved.': '© 2026 Gerlyn Variety Store · Baliuag, Bulacan · Nakalaan ang lahat ng karapatan.',

        # ── Settings sidebar ─────────────────────────────────────────
        'My Profile': 'Aking Profile',
        'Addresses': 'Mga Address',
        'Sessions': 'Mga Session',
        'Notifications': 'Mga Abiso',
        'Privacy & Data': 'Privacy at Data',
        'Language': 'Wika',
        'Help Center': 'Sentro ng Tulong',
        'About': 'Tungkol Sa',
        'More': 'Higit Pa',

        # ── Settings hub — shared header ─────────────────────────────
        'Manage your profile, addresses, and preferences': 'Pamahalaan ang iyong profile, mga address, at mga kagustuhan',

        # ── Profile page ─────────────────────────────────────────────
        'Personal Information': 'Personal na Impormasyon',
        'Password & Security': 'Password at Seguridad',
        'Edit': 'I-edit',
        'Change': 'Baguhin',
        'Cancel': 'Kanselahin',
        'Save changes': 'I-save ang mga Pagbabago',
        'Full Name': 'Buong Pangalan',
        'Mobile Number': 'Numero ng Mobile',
        'Email Address': 'Email Address',
        'Username': 'Username',
        'Delivery Address': 'Delivery Address',
        'Add another address': 'Magdagdag ng ibang address',
        'Two-Factor Authentication': 'Two-Factor Authentication',
        'Password': 'Password',
        'First Name': 'Pangalan',
        'Last Name': 'Apelyido',
        'Not set': 'Wala pang naitakda',
        'No address saved': 'Walang naka-save na address',
        'Save password': 'I-save ang Password',
        'Current Password': 'Kasalukuyang Password',
        'New Password': 'Bagong Password',
        'Confirm New Password': 'Kumpirmahin ang Bagong Password',
        'Confirm Your Password': 'Kumpirmahin ang Iyong Password',
        'Confirm': 'Kumpirmahin',

        # ── Addresses page ───────────────────────────────────────────
        'Saved Addresses': 'Mga Naka-save na Address',
        'Add Address': 'Magdagdag ng Address',
        'Set as default': 'Gawing default',
        'Save Address': 'I-save ang Address',
        'No saved addresses yet. Add your first delivery address above.': 'Wala pang naka-save na address. Idagdag ang iyong unang delivery address sa itaas.',
        'Default': 'Default',
        'This is your default delivery address. Add a new address and set it default to change it.': 'Ito ang iyong default na delivery address. Magdagdag ng bagong address at gawin itong default para baguhin ito.',
        'Edit Address': 'I-edit ang Address',

        # ── Address map picker modal ─────────────────────────────────
        'Home': 'Bahay',
        'Work': 'Trabaho',
        'Other': 'Iba pa',
        'Region': 'Rehiyon',
        'Province': 'Probinsya',
        'City / Municipality': 'Lungsod / Munisipyo',
        'Barangay': 'Barangay',
        'Street name, Building, House No.': 'Pangalan ng Kalye, Building, Bilang ng Bahay',
        'Tap or drag the pin to mark the exact spot — the address below updates to match.': 'I-tap o i-drag ang pin para itakda ang eksaktong lokasyon — ma-a-update ang address sa ibaba ayon dito.',
        'Full Address': 'Buong Address',

        # ── Notification Preferences page ────────────────────────────
        'Notification Preferences': 'Kagustuhan sa Abiso',
        'Order updates': 'Update sa Order',
        'Status changes and delivery confirmations': 'Mga pagbabago sa status at kumpirmasyon ng delivery',
        'Promotions & deals': 'Mga Promo at Deal',
        'Discounts and flash sales': 'Mga diskwento at flash sale',
        'Delivery reminders': 'Paalala sa Delivery',
        'Reminders before your order arrives': 'Paalala bago dumating ang iyong order',
        'Newsletter': 'Newsletter',
        'Monthly store news and updates': 'Buwanang balita at update ng tindahan',

        # ── Language settings page ───────────────────────────────────
        'Choose the language you\'d like to use across the store.': 'Piliin ang wikang gusto mong gamitin sa buong store.',
        'Save': 'I-save',
        'English': 'English',
        'Filipino': 'Filipino',

        # ── Help Center ───────────────────────────────────────────────
        'Still need help?': 'Kailangan pa ng tulong?',

        # ── Settings placeholders (Sessions / Privacy & Data) ─────────
        'This section is coming soon.': 'Paparating na ang seksyong ito.',
        'See and manage devices that are signed in to your account.': 'Tingnan at pamahalaan ang mga device na naka-sign in sa iyong account.',
        'Control what data we keep, download a copy of it, or delete your account.': 'Kontrolin ang data na iniimbak namin, mag-download ng kopya nito, o burahin ang iyong account.',

        # ── About page ────────────────────────────────────────────────
        'About Gerlyn Variety Store': 'Tungkol sa Gerlyn Variety Store',
        'Serving Baliuag, Bulacan and nearby communities.': 'Naglilingkod sa Baliuag, Bulacan at mga karatig-lugar.',
        'Our Branches': 'Aming mga Sangay',
        'Delivery': 'Delivery',
        'Free delivery within Baliuag on orders over ₱500.': 'Libreng delivery sa loob ng Baliuag para sa orders na ₱500 pataas.',
        'Payment Methods': 'Mga Paraan ng Pagbabayad',
        'Need Help?': 'Kailangan ng Tulong?',
        'Visit our': 'Bisitahin ang aming',
        'or drop by any branch above.': 'o pumunta sa alinman sa mga sangay sa itaas.',

        # ── Help Center FAQ ───────────────────────────────────────────
        'How do I change my password?': 'Paano ko babaguhin ang aking password?',
        "Go to Settings → My Profile → Password & Security → Change. If Two-Factor Authentication is on, we'll email you a secure link to confirm the change instead of changing it right away.":
            "Pumunta sa Settings → My Profile → Password & Security → Change. Kung naka-on ang Two-Factor Authentication, magpapadala kami ng secure na link sa iyong email para kumpirmahin ang pagbabago sa halip na direktang baguhin ito.",

        'What is Two-Factor Authentication and how do I turn it on?': 'Ano ang Two-Factor Authentication at paano ko ito i-o-on?',
        "It's an extra security step: once enabled, password changes must be confirmed via a link sent to your email, not just the current-password form. Toggle it from Settings → My Profile → Password & Security — you'll be asked to confirm your current password first.":
            "Isa itong karagdagang hakbang sa seguridad: kapag naka-enable, kailangang kumpirmahin ang mga pagbabago sa password sa pamamagitan ng link na ipapadala sa iyong email, hindi lang sa current-password form. I-toggle ito mula sa Settings → My Profile → Password & Security — hihingin muna ang iyong current password para kumpirmahin.",

        'How do I add or change my delivery address?': 'Paano ako magdadagdag o magbabago ng aking delivery address?',
        "Go to Settings → Addresses → Add Address. Pick your Region, Province, City/Municipality, and Barangay, then drag the pin on the map to your exact spot — the address text updates to match. Your default address is the one used for delivery; to change it, add a new address and set it as default.":
            "Pumunta sa Settings → Addresses → Add Address. Piliin ang iyong Rehiyon, Probinsya, Lungsod/Munisipyo, at Barangay, pagkatapos i-drag ang pin sa mapa papunta sa eksaktong lokasyon — ma-a-update ang address text ayon dito. Ang default address ang gagamitin para sa delivery; para baguhin ito, magdagdag ng bagong address at gawin itong default.",

        'What payment methods do you accept?': 'Anong mga paraan ng pagbabayad ang tinatanggap ninyo?',
        'Cash, GCash, Card, Bank Transfer, Cash on Delivery, and Pay at Store.': 'Cash, GCash, Card, Bank Transfer, Cash on Delivery, at Pay at Store.',

        'Do you deliver outside Baliuag?': 'Naghahatid ba kayo sa labas ng Baliuag?',
        'Delivery is free within Baliuag on orders over ₱500. For areas outside Baliuag, please reach out to your nearest branch (see the About page) to confirm coverage and delivery fees.':
            'Libre ang delivery sa loob ng Baliuag para sa orders na ₱500 pataas. Para sa mga lugar sa labas ng Baliuag, makipag-ugnayan sa pinakamalapit na sangay (tingnan ang About page) para kumpirmahin ang coverage at delivery fee.',

        'How do I control what notifications I get?': 'Paano ko kokontrolin ang mga abisong natatanggap ko?',
        'Go to Settings → Notifications to turn order updates, promotions, delivery reminders, and the newsletter on or off individually.':
            'Pumunta sa Settings → Notifications para i-on o i-off ang order updates, promotions, delivery reminders, at newsletter nang paisa-isa.',

        'Visit or call your nearest Gerlyn Variety Store branch — Tarcan, Makinabang, or Tiaong Baliwag Mini Mart — listed on the About page.':
            'Bisitahin o tawagan ang pinakamalapit na sangay ng Gerlyn Variety Store — Tarcan, Makinabang, o Tiaong Baliwag Mini Mart — na nakalista sa About page.',

        # ── Sessions page ─────────────────────────────────────────────
        'This device': 'Ang device na ito',
        'Log out': 'Mag-log Out',
        'Log out all other sessions': 'I-log Out ang Lahat ng Ibang Session',
        'Log out of all other sessions?': 'I-log out ang lahat ng ibang session?',
        'Last active': 'Huling aktibo',
        'ago': 'ang nakaraan',
        'No other active sessions.': 'Walang ibang aktibong session.',
        'Unknown device': 'Hindi kilalang device',

        # ── Privacy & Data / account deactivation ──────────────────────
        'Deactivate Account': 'I-deactivate ang Account',
        'Signs you out everywhere and scrubs your personal info. Your order history stays intact but is no longer linked to your name.':
            'Ilo-log out ka sa lahat ng device at buburahin ang iyong personal na impormasyon. Mananatili ang iyong order history pero hindi na ito naka-link sa iyong pangalan.',
        "This deactivates your account: you'll be signed out everywhere, your profile info is scrubbed, and you won't be able to log back in. It cannot be easily undone.":
            'Ide-deactivate nito ang iyong account: ma-lo-log out ka sa lahat ng device, mabubura ang impormasyon ng iyong profile, at hindi ka na makaka-log in ulit. Hindi ito madaling maibabalik.',
        'Are you absolutely sure you want to deactivate your account? This cannot be easily undone.':
            'Sigurado ka bang gusto mong i-deactivate ang iyong account? Hindi ito madaling maibabalik.',
        'Enter your password': 'Ilagay ang iyong password',

        # ── Download / PWA page ─────────────────────────────────────────
        'Download App': 'I-download ang App',
        'Shop online, track orders, and manage your account — right from your home screen.':
            'Mamili online, subaybayan ang mga order, at pamahalaan ang iyong account — direkta mula sa iyong home screen.',
        'Version': 'Bersyon',
        'Download': 'I-download',
        'Update': 'I-update',
        'Tap the Share icon in Safari, then tap Add to Home Screen.':
            'I-tap ang Share icon sa Safari, pagkatapos i-tap ang Add to Home Screen.',
        "What's New": 'Mga Bagong Update',
        'Recent updates': 'Mga kamakailang update',
        'Installed! You can now open this from your home screen.':
            'Na-install na! Puwede mo na itong buksan mula sa iyong home screen.',
        'Your browser does not support installing this app, or it is already installed.':
            'Hindi suportado ng iyong browser ang pag-install ng app na ito, o naka-install na ito.',
        'Installing…': 'Ini-install…',
        'Checking for updates…': 'Sinusuri ang mga update…',
        'Update found — refreshing…': 'May nahanap na update — nire-refresh…',
        'You are already up to date.': 'Ikaw ay nasa pinakabagong bersyon na.',
        'Could not check for updates right now.': 'Hindi ma-check ang mga update sa ngayon.',
    },
}


def translate(text, language):
    if not language or language == 'en':
        return text
    return TRANSLATIONS.get(language, {}).get(text, text)
