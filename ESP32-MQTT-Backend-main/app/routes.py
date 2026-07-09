from flask import (
    abort,
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .auth import (
    admin_required,
    csrf_is_valid,
    csrf_token,
    current_admin,
    log_in_admin,
    log_out_admin,
)
from .extensions import db
from .models import Admin, Device, utc_now


site_bp = Blueprint("site", __name__)


@site_bp.get("/")
def index():
    endpoint = "site.dashboard" if current_admin() else "site.login"
    return redirect(url_for(endpoint))


@site_bp.route("/admin/login", methods=["GET", "POST"])
def login():
    if current_admin() is not None:
        return redirect(url_for("site.dashboard"))

    if request.method == "POST":
        if not csrf_is_valid():
            flash("页面已过期，请重新提交。", "error")
            return render_template("login.html", csrf_token=csrf_token()), 400

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        admin = db.session.scalar(
            db.select(Admin).where(Admin.username == username)
        )

        if admin is None or not admin.check_password(password):
            flash("管理员账号或密码错误。", "error")
        else:
            admin.last_login_at = utc_now()
            db.session.commit()
            log_in_admin(admin)
            return redirect(url_for("site.dashboard"))

    return render_template("login.html", csrf_token=csrf_token())


@site_bp.post("/admin/logout")
@admin_required
def logout():
    if not csrf_is_valid():
        flash("退出请求已过期，请重试。", "error")
        return redirect(url_for("site.dashboard"))

    log_out_admin()
    return redirect(url_for("site.login"))


@site_bp.get("/admin")
@admin_required
def dashboard():
    return render_template(
        "dashboard.html",
        admin=current_admin(),
        csrf_token=csrf_token(),
    )


@site_bp.get("/admin/devices/<string:device_name>/dashboard")
@admin_required
def device_dashboard(device_name: str):
    device = db.session.scalar(
        db.select(Device).where(Device.device_name == device_name)
    )
    if device is None:
        abort(404)
    return render_template(
        "device_dashboard.html",
        admin=current_admin(),
        csrf_token=csrf_token(),
        device=device,
    )


@site_bp.get("/health")
def health():
    return {"status": "ok"}
