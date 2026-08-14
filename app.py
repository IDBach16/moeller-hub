"""
Moeller Baseball Analytics Hub
Central landing page for all Moeller Baseball analytics tools.
"""

import os
import subprocess
from datetime import timedelta
from flask import (Flask, render_template_string, send_from_directory,
                   jsonify, request, session, redirect, url_for)

app = Flask(__name__)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------

app.secret_key = os.environ.get("SECRET_KEY", "moeller-hub-2027-secret")
app.permanent_session_lifetime = timedelta(days=30)

# Empty = no gate. To require a password again, set HUB_PASSWORD on Railway.
HUB_PASSWORD = os.environ.get("HUB_PASSWORD", "")

# routes that never require login (assets used by the login page itself)
PUBLIC_PATHS = {"/login", "/bg-field.jpg", "/shield.png", "/moeller-logo.png",
                "/favicon.ico", "/manifest.json"}

@app.before_request
def require_login():
    if not HUB_PASSWORD:
        return None
    if request.path in PUBLIC_PATHS:
        return None
    if not session.get("authed"):
        return redirect(url_for("login"))
    return None

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Moeller Baseball Analytics — Login</title>
<link rel="icon" href="/moeller-logo.png"/>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --navy:#1a1a2e; --navy-deep:#0f0f1e;
  --gold:#C5A55A; --gold-light:#d4ba78;
  --white:#ffffff;
  --glass:rgba(26,26,46,.65); --glass-border:rgba(197,165,90,.25);
}
body{
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:var(--navy-deep);color:var(--white);
}
body::before{
  content:'';position:fixed;inset:0;
  background:url('/bg-field.jpg') center/cover no-repeat;
  filter:brightness(.3) saturate(.8);z-index:0;
}
.login-card{
  position:relative;z-index:1;
  width:min(400px,90vw);
  background:var(--glass);
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  border:1px solid var(--glass-border);
  border-radius:16px;
  padding:2.5rem 2rem;
  text-align:center;
  box-shadow:0 20px 60px rgba(0,0,0,.5);
}
.login-card img{
  width:90px;height:90px;object-fit:contain;margin-bottom:1rem;
  filter:drop-shadow(0 0 25px rgba(197,165,90,.4));
}
.login-title{
  font-size:1.1rem;font-weight:900;letter-spacing:.12em;text-transform:uppercase;
  background:linear-gradient(135deg,var(--white),var(--gold),var(--white));
  background-size:200% auto;-webkit-background-clip:text;-webkit-text-fill-color:transparent;
  margin-bottom:.4rem;
}
.login-sub{
  font-size:.7rem;letter-spacing:.3em;text-transform:uppercase;
  color:var(--gold);opacity:.85;margin-bottom:1.8rem;
}
input[type=password]{
  width:100%;padding:.85rem 1rem;
  background:rgba(15,15,30,.7);
  border:1px solid var(--glass-border);
  border-radius:8px;color:var(--white);
  font-size:1rem;letter-spacing:.05em;
  outline:none;margin-bottom:1rem;
  transition:border-color .3s ease;
}
input[type=password]:focus{border-color:var(--gold)}
button{
  width:100%;padding:.85rem 1rem;
  background:transparent;border:1.5px solid var(--gold);border-radius:8px;
  color:var(--gold);font-size:.85rem;font-weight:600;
  letter-spacing:.1em;text-transform:uppercase;cursor:pointer;
  transition:all .3s ease;
}
button:hover{background:var(--gold);color:var(--navy)}
.err{
  color:#e37f7f;font-size:.8rem;margin-bottom:1rem;letter-spacing:.03em;
}
</style>
</head>
<body>
<form class="login-card" method="POST" action="/login">
  <img src="/shield.png" alt="Moeller Shield"/>
  <div class="login-title">Moeller Baseball Analytics</div>
  <div class="login-sub">Coaches Access</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="password" name="password" placeholder="Password" autofocus required/>
  <button type="submit">Enter</button>
</form>
</body>
</html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if not HUB_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == HUB_PASSWORD:
            session.permanent = True
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Incorrect password"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Static file routes
# ---------------------------------------------------------------------------

@app.route("/bg-field.jpg")
def bg_field():
    return send_from_directory(APP_DIR, "bg-field.jpg")

@app.route("/shield.png")
def shield():
    return send_from_directory(APP_DIR, "shield.png")

@app.route("/moeller-logo.png")
def logo():
    return send_from_directory(APP_DIR, "moeller-logo.png")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(APP_DIR, "moeller-logo.png")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(APP_DIR, "manifest.json")

# ---------------------------------------------------------------------------
# Git push endpoint
# ---------------------------------------------------------------------------

@app.route("/api/git-push", methods=["POST"])
def git_push():
    # The password gate is what kept this endpoint private. With the gate off,
    # it must not be reachable at all.
    if not HUB_PASSWORD:
        return jsonify({"ok": False, "error": "disabled while the password gate is off"}), 403
    try:
        result = subprocess.run(
            ["git", "add", "-A"],
            cwd=APP_DIR, capture_output=True, text=True,
        )
        msg = request.json.get("message", "auto-push") if request.is_json else "auto-push"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=APP_DIR, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "push"],
            cwd=APP_DIR, capture_output=True, text=True,
        )
        return jsonify({"ok": True, "output": result.stdout or result.stderr})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Moeller Baseball Analytics</title>
<link rel="icon" href="/moeller-logo.png"/>
<link rel="apple-touch-icon" href="/moeller-logo.png"/>
<link rel="manifest" href="/manifest.json"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="apple-mobile-web-app-title" content="Moeller Baseball"/>
<meta name="theme-color" content="#1a1a2e"/>
<style>
/* ===== RESET & BASE ===== */
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --navy:#1a1a2e;
  --navy-deep:#0f0f1e;
  --gold:#C5A55A;
  --gold-light:#d4ba78;
  --gold-dim:rgba(197,165,90,.15);
  --white:#ffffff;
  --glass:rgba(26,26,46,.55);
  --glass-border:rgba(197,165,90,.2);
}
html{scroll-behavior:smooth;font-size:16px}
body{
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  background:var(--navy-deep);
  color:var(--white);
  overflow-x:hidden;
  -webkit-font-smoothing:antialiased;
}

/* ===== HERO ===== */
.hero{
  position:relative;
  min-height:70vh;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  text-align:center;
  overflow:hidden;
}
.hero::before{
  content:'';
  position:absolute;inset:0;
  background:url('/bg-field.jpg') center/cover no-repeat fixed;
  filter:brightness(.35) saturate(.8);
  z-index:0;
}
.hero::after{
  content:'';
  position:absolute;inset:0;
  background:linear-gradient(
    180deg,
    rgba(15,15,30,.6) 0%,
    rgba(15,15,30,.3) 40%,
    rgba(15,15,30,.85) 100%
  );
  z-index:1;
}
.hero-content{position:relative;z-index:2;padding:2rem 1rem}

/* Shield logo */
.shield-logo{
  width:140px;height:140px;
  object-fit:contain;
  margin-bottom:1.5rem;
  filter:drop-shadow(0 0 30px rgba(197,165,90,.4));
  animation:shieldPulse 4s ease-in-out infinite;
}
@keyframes shieldPulse{
  0%,100%{filter:drop-shadow(0 0 20px rgba(197,165,90,.3))}
  50%{filter:drop-shadow(0 0 40px rgba(197,165,90,.6))}
}

.hero-title{
  font-size:clamp(2rem,5vw,3.5rem);
  font-weight:900;
  letter-spacing:.12em;
  text-transform:uppercase;
  background:linear-gradient(135deg,var(--white) 0%,var(--gold) 50%,var(--white) 100%);
  background-size:200% auto;
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  animation:shimmer 6s linear infinite;
  margin-bottom:.5rem;
}
@keyframes shimmer{
  0%{background-position:0% center}
  100%{background-position:200% center}
}

/* Divider */
.hero-divider{
  width:80px;height:2px;
  background:linear-gradient(90deg,transparent,var(--gold),transparent);
  margin:1.2rem auto;
}

/* Scroll indicator -- centred with left/right:0 rather than a translateX,
   because the fadeInUp animation's final transform would overwrite it and
   push the hint off-centre. */
.scroll-hint{
  position:absolute;bottom:2rem;left:0;right:0;
  z-index:2;
  display:flex;flex-direction:column;align-items:center;gap:.4rem;
  opacity:.5;animation:fadeInUp 1s .8s both;
}
.scroll-hint span{font-size:.7rem;letter-spacing:.15em;text-transform:uppercase;color:var(--gold-light)}
.scroll-arrow{
  width:20px;height:20px;
  border-right:2px solid var(--gold);border-bottom:2px solid var(--gold);
  transform:rotate(45deg);
  animation:bounce 2s infinite;
}
@keyframes bounce{
  0%,100%{transform:rotate(45deg) translateY(0)}
  50%{transform:rotate(45deg) translateY(6px)}
}

/* ===== CARDS SECTION ===== */
.section{
  max-width:1200px;
  margin:0 auto;
  padding:4rem 1.5rem 5rem;
}
.section-label{
  text-align:center;
  font-size:.75rem;
  letter-spacing:.3em;
  text-transform:uppercase;
  color:var(--gold);
  margin-bottom:.5rem;
}
.section-title{
  text-align:center;
  font-size:clamp(1.5rem,3vw,2.2rem);
  font-weight:700;
  margin-bottom:2.5rem;
  color:var(--white);
}

.cards-grid{
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:1.5rem;
}

/* ===== CARD ===== */
.card{
  position:relative;
  background:var(--glass);
  backdrop-filter:blur(16px);
  -webkit-backdrop-filter:blur(16px);
  border:1px solid var(--glass-border);
  border-radius:16px;
  padding:2rem 1.5rem 1.8rem;
  display:flex;flex-direction:column;
  transition:transform .35s cubic-bezier(.22,1,.36,1),
             box-shadow .35s ease,
             border-color .35s ease;
  overflow:hidden;
  opacity:0;transform:translateY(30px);
}
.card.visible{
  opacity:1;transform:translateY(0);
  transition:opacity .6s ease,transform .6s cubic-bezier(.22,1,.36,1);
}
.card::before{
  content:'';
  position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--gold),var(--gold-light),var(--gold));
  opacity:0;transition:opacity .35s ease;
}
.card:hover{
  transform:translateY(-6px);
  box-shadow:0 20px 50px rgba(0,0,0,.4),0 0 30px rgba(197,165,90,.08);
  border-color:rgba(197,165,90,.35);
}
.card:hover::before{opacity:1}

.card-icon{
  width:52px;height:52px;
  border-radius:12px;
  display:flex;align-items:center;justify-content:center;
  background:var(--gold-dim);
  margin-bottom:1.2rem;
  flex-shrink:0;
}
.card-icon svg{width:26px;height:26px;stroke:var(--gold);fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}

.card-title{
  font-size:1.15rem;
  font-weight:700;
  margin-bottom:.5rem;
  color:var(--white);
}
.card-desc{
  font-size:.88rem;
  line-height:1.6;
  color:rgba(255,255,255,.6);
  flex:1;
  margin-bottom:1.4rem;
}

.card-btn{
  display:inline-flex;align-items:center;gap:.5rem;
  padding:.65rem 1.4rem;
  background:transparent;
  border:1.5px solid var(--gold);
  border-radius:8px;
  color:var(--gold);
  font-size:.82rem;
  font-weight:600;
  letter-spacing:.08em;
  text-transform:uppercase;
  text-decoration:none;
  cursor:pointer;
  transition:all .3s ease;
  align-self:flex-start;
}
.card-btn:hover{
  background:var(--gold);
  color:var(--navy);
  box-shadow:0 4px 20px rgba(197,165,90,.3);
}
.card-btn svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2}

/* Coming soon badge */
.badge{
  position:absolute;top:1rem;right:1rem;
  background:linear-gradient(135deg,var(--gold),var(--gold-light));
  color:var(--navy);
  font-size:.65rem;
  font-weight:700;
  letter-spacing:.1em;
  text-transform:uppercase;
  padding:.3rem .7rem;
  border-radius:6px;
}

/* ===== FOOTER ===== */
.footer{
  text-align:center;
  padding:2.5rem 1rem;
  border-top:1px solid rgba(197,165,90,.12);
  font-size:.78rem;
  color:rgba(255,255,255,.35);
  letter-spacing:.06em;
}
.footer span{color:var(--gold);opacity:.6}

/* ===== RESPONSIVE ===== */
@media(max-width:960px){
  .cards-grid{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:600px){
  .cards-grid{grid-template-columns:1fr}
  .hero{min-height:60vh}
  .shield-logo{width:100px;height:100px}
  .section{padding:3rem 1rem 4rem}
}

/* ===== ENTRANCE ANIMATIONS ===== */
@keyframes fadeInUp{
  from{opacity:0;transform:translateY(20px)}
  to{opacity:1;transform:translateY(0)}
}
.hero-content>*{animation:fadeInUp .8s ease both}
.hero-content>*:nth-child(1){animation-delay:.1s}
.hero-content>*:nth-child(2){animation-delay:.25s}
.hero-content>*:nth-child(3){animation-delay:.4s}
.hero-content>*:nth-child(4){animation-delay:.55s}
</style>
</head>
<body>

<!-- ====== HERO ====== -->
<section class="hero">
  <div class="hero-content">
    <img src="/shield.png" alt="Moeller Shield" class="shield-logo"/>
    <div class="hero-divider"></div>
    <h1 class="hero-title">Moeller Baseball Analytics</h1>
  </div>
  <div class="scroll-hint">
    <span>Explore Tools</span>
    <div class="scroll-arrow"></div>
  </div>
</section>

<!-- ====== TOOLS SECTION ====== -->
<section class="section" id="tools">
  <p class="section-label">Analytics Suite</p>
  <h2 class="section-title">Your Competitive Edge</h2>

  <div class="cards-grid">

    <!-- 1. Game Prep Agent -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><line x1="2" y1="12" x2="22" y2="12"/></svg>
      </div>
      <h3 class="card-title">Scouting Agent</h3>
      <p class="card-desc">Our scouting tool that allows you to ask questions and get information from the data we have collected.</p>
      <a href="https://web-production-510f.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 2. Pitcher Cards -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/><path d="M8 2s1.5 2 4 2 4-2 4-2"/></svg>
      </div>
      <h3 class="card-title">Pitcher Cards</h3>
      <p class="card-desc">Pitcher information and updates for quick reference before and during games.</p>
      <a href="https://web-production-08767.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 3. Hitter Cards -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><path d="M4 20h16"/><path d="M4 20V10l4-6h8l4 6v10"/><rect x="8" y="12" width="8" height="8" rx="1"/><line x1="12" y1="12" x2="12" y2="8"/></svg>
      </div>
      <h3 class="card-title">Hitter Cards</h3>
      <p class="card-desc">Hitter information and updates that can be used for player evaluation, planning, and in-game reference.</p>
      <a href="https://web-production-51eb5b.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 4. Umpire Cards -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
      </div>
      <h3 class="card-title">Umpire Cards</h3>
      <p class="card-desc">Quick reference tool for umpire information and game-use situations.</p>
      <a href="https://web-production-196103.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 5. Team Stats -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><path d="M18 20V10M12 20V4M6 20v-6"/></svg>
      </div>
      <h3 class="card-title">Team Stats</h3>
      <p class="card-desc">Full team batting and pitching stats dashboard with leaderboards and Synergy scouting.</p>
      <p style="font-size:12px;color:#C5A55A;margin-top:6px;">Login: <strong>moeller</strong> &nbsp;|&nbsp; Password: <strong>moeller1</strong></p>
      <a href="https://moeller-2026-stats-production.up.railway.app/login" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 6. AWRE Video Search -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      </div>
      <h3 class="card-title">AWRE Video Search</h3>
      <p class="card-desc">Search game video by team, player, pitch type, and result. Filter 9,600+ pitches across 45 games with multi-angle playback.</p>
      <a href="https://web-production-12b79.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 7. Charting App -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8.5 4.5c2 3 2 12 0 15"/><path d="M15.5 4.5c-2 3-2 12 0 15"/></svg>
      </div>
      <h3 class="card-title">Charting App</h3>
      <p class="card-desc">Pitch-by-pitch charting for off-season bullpens and live ABs. Tap the attack zone, log the pitch, and the coach dashboard updates behind it.</p>
      <a href="https://moeller-charting-production.up.railway.app/" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

    <!-- 8. Pitch Overlays -->
    <div class="card">
      <div class="card-icon">
        <svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><path d="M7 10l3 3 7-7" stroke-width="2"/></svg>
      </div>
      <h3 class="card-title">Pitch Overlays</h3>
      <p class="card-desc">Delivery overlay comparisons by pitcher. Side-by-side and stacked views synced to release point for mechanical analysis.</p>
      <a href="https://drive.google.com/drive/folders/1gruNdqaNpmhgRp2_4qdidSIRP12vnfkh?usp=sharing" target="_blank" class="card-btn">
        Launch <svg viewBox="0 0 24 24"><path d="M7 17L17 7M17 7H7M17 7v10"/></svg>
      </a>
    </div>

  </div>
</section>

<!-- ====== FOOTER ====== -->
<footer class="footer">
  Moeller Baseball Analytics <span>|</span> @MoeAnalytics
</footer>

<!-- ====== SCROLL ANIMATIONS ====== -->
<script>
(function(){
  const cards=document.querySelectorAll('.card');
  const observer=new IntersectionObserver((entries)=>{
    entries.forEach((entry,i)=>{
      if(entry.isIntersecting){
        setTimeout(()=>{entry.target.classList.add('visible')},i*100);
        observer.unobserve(entry.target);
      }
    });
  },{threshold:0.15});
  cards.forEach(c=>observer.observe(c));
})();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, host="0.0.0.0", port=port)
