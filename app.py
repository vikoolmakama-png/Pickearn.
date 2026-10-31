import os
import secrets
import string
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from decimal import Decimal, InvalidOperation

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

def db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(
            database_url,
            sslmode="require",
        )

    return psycopg2.connect(
        dbname=os.environ.get("DB_NAME", "pickearn"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432")
    )

def make_referral():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        phone = request.form.get('phone', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        referral = request.form.get('referral', '').strip().upper()

        if not phone or not username or not password:
            flash('Please complete all required fields.')
            return redirect('/register')

        if password != confirm:
            flash('Passwords do not match.')
            return redirect('/register')

        if len(password) < 8:
            flash('Password must contain at least 8 characters.')
            return redirect('/register')

        try:
            with db() as conn:
                with conn.cursor() as cur:
                    referred_by = None

                    if referral:
                        cur.execute(
                            "SELECT id FROM users WHERE referral_code=%s",
                            (referral,)
                        )
                        referrer = cur.fetchone()
                        if not referrer:
                            flash('Invalid referral code.')
                            return redirect('/register')
                        referred_by = referrer[0]

                    code = make_referral()
                    cur.execute("""
                        INSERT INTO users
                        (phone, username, password_hash, referral_code, referred_by)
                        VALUES (%s,%s,%s,%s,%s)
                        RETURNING id
                    """, (
                        phone, username,
                        generate_password_hash(password),
                        code, referred_by
                    ))
                    user_id = cur.fetchone()[0]

            session['user_id'] = user_id
            return redirect('/dashboard')

        except psycopg2.errors.UniqueViolation:
            flash('Phone number or username already exists.')
        except Exception:
            app.logger.exception("Registration failed")
            flash('Registration failed. Please try again.')

        return redirect('/register')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        try:
            with db() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT id, password_hash
                        FROM users
                        WHERE username=%s OR phone=%s
                    """, (username, username))
                    user = cur.fetchone()

            if user and check_password_hash(user['password_hash'], password):
                session.clear()
                session['user_id'] = user['id']
                return redirect('/dashboard')

            flash('Incorrect username or password.')

        except Exception:
            app.logger.exception("Login failed")
            flash('Login service temporarily unavailable.')

        return redirect('/login')

    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/login')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, balance, referral_code FROM users WHERE id=%s",
                (session['user_id'],)
            )
            user = cur.fetchone()

            if not user:
                session.clear()
                return redirect('/login')

            cur.execute("""
                SELECT p.id, p.name, p.price, p.reward_per_task
                FROM user_plans up
                JOIN plans p ON p.id=up.plan_id
                WHERE up.user_id=%s AND up.status='active'
                ORDER BY up.id DESC LIMIT 1
            """, (session['user_id'],))
            plan = cur.fetchone()

            cur.execute("""
                SELECT id, name, price, reward_per_task
                FROM plans WHERE active=TRUE ORDER BY price
            """)
            plans = cur.fetchall()

    return render_template(
        'dashboard.html',
        user=user,
        plan=plan,
        plans=plans
    )




@app.route('/activate-plan/<int:plan_id>', methods=['POST'])
def activate_plan(plan_id):
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    try:
        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, balance, referred_by
                    FROM users WHERE id=%s FOR UPDATE
                """, (user_id,))
                user = cur.fetchone()

                if not user:
                    flash('Account not found.')
                    return redirect('/login')

                cur.execute("""
                    SELECT id FROM user_plans
                    WHERE user_id=%s AND status='active'
                    LIMIT 1
                """, (user_id,))

                if cur.fetchone():
                    flash('You already have an active plan.')
                    return redirect('/dashboard')

                cur.execute("""
                    SELECT id, name, price
                    FROM plans
                    WHERE id=%s AND active=TRUE
                """, (plan_id,))
                plan = cur.fetchone()

                if not plan:
                    flash('Selected plan does not exist.')
                    return redirect('/dashboard')

                if user['balance'] < plan['price']:
                    flash('Insufficient balance. Please deposit first.')
                    return redirect('/dashboard')

                cur.execute("""
                    UPDATE users
                    SET balance=balance-%s
                    WHERE id=%s AND balance >= %s
                    RETURNING balance
                """, (plan['price'], user_id, plan['price']))

                if not cur.fetchone():
                    flash('Insufficient balance.')
                    return redirect('/dashboard')

                cur.execute("""
                    INSERT INTO user_plans
                    (user_id, plan_id, status, activated_at)
                    VALUES (%s,%s,'active',CURRENT_TIMESTAMP)
                """, (user_id, plan['id']))

                cur.execute("""
                    INSERT INTO transactions
                    (user_id, amount, transaction_type, reference)
                    VALUES (%s,%s,'plan_activation',%s)
                """, (
                    user_id,
                    -plan['price'],
                    'PLAN-' + secrets.token_hex(12)
                ))

                # Pay the referrer once, after successful activation.
                if user['referred_by']:
                    commission = plan['price'] * Decimal('0.50')

                    cur.execute("""
                        INSERT INTO referral_commissions
                        (referrer_user_id, referred_user_id, plan_id, amount)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (referred_user_id) DO NOTHING
                        RETURNING id
                    """, (
                        user['referred_by'],
                        user_id,
                        plan['id'],
                        commission
                    ))

                    commission_record = cur.fetchone()

                    if commission_record:
                        cur.execute("""
                            UPDATE users
                            SET balance=balance+%s
                            WHERE id=%s
                        """, (commission, user['referred_by']))

                        cur.execute("""
                            INSERT INTO transactions
                            (user_id, amount, transaction_type, reference)
                            VALUES (%s,%s,'referral_commission',%s)
                        """, (
                            user['referred_by'],
                            commission,
                            'REF-' + secrets.token_hex(12)
                        ))

        flash(
            f'{plan["name"]} activated successfully! '
            'Your referrer has received their 50% commission.'
        )

    except Exception:
        app.logger.exception("Plan activation error")
        flash('Activation failed. Please try again.')

    return redirect('/dashboard')


@app.route('/tasks')
def tasks():
    if 'user_id' not in session:
        return redirect('/login')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.name, p.reward_per_task
                FROM user_plans up
                JOIN plans p ON p.id=up.plan_id
                WHERE up.user_id=%s AND up.status='active'
                ORDER BY up.id DESC LIMIT 1
            """, (session['user_id'],))
            plan = cur.fetchone()

            if not plan:
                flash('Activate a plan before accessing tasks.')
                return redirect('/dashboard')

            cur.execute("""
                SELECT t.id, t.title, t.description, t.task_link
                FROM tasks t
                WHERE t.active=TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM task_submissions s
                    WHERE s.task_id=t.id AND s.user_id=%s
                  )
                ORDER BY t.id DESC
            """, (session['user_id'],))
            available_tasks = cur.fetchall()

    return render_template(
        'tasks.html',
        tasks=available_tasks,
        plan=plan
    )


@app.route('/submit-task/<int:task_id>', methods=['POST'])
def submit_task(task_id):
    if 'user_id' not in session:
        return redirect('/login')

    submitted_username = request.form.get('submitted_username', '').strip()
    if not submitted_username or len(submitted_username) > 100:
        flash('Enter a valid username for this task.')
        return redirect('/tasks')

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM user_plans
                    WHERE user_id=%s AND status='active'
                    LIMIT 1
                """, (session['user_id'],))
                if not cur.fetchone():
                    flash('You need an active plan to submit tasks.')
                    return redirect('/dashboard')

                cur.execute("""
                    INSERT INTO task_submissions
                    (user_id, task_id, submitted_username, status)
                    SELECT %s, id, %s, 'pending'
                    FROM tasks WHERE id=%s AND active=TRUE
                """, (session['user_id'], submitted_username, task_id))

                if cur.rowcount == 0:
                    flash('This task is no longer available.')
                    return redirect('/tasks')

        flash('Task submitted. It will disappear while awaiting admin review.')

    except __import__('psycopg2').errors.UniqueViolation:
        flash('You have already submitted this task.')
    except Exception:
        app.logger.exception("Task submission failed")
        flash('Could not submit this task. Please try again.')

    return redirect('/tasks')


@app.route('/withdraw', methods=['GET', 'POST'])
def withdraw():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    if request.method == 'POST':
        bank_name = request.form.get('bank_name', '').strip()
        account_number = request.form.get('account_number', '').strip()
        account_name = request.form.get('account_name', '').strip()

        try:
            amount = Decimal(request.form.get('amount', '0'))
        except InvalidOperation:
            flash('Enter a valid withdrawal amount.')
            return redirect('/withdraw')

        if not bank_name or not account_name:
            flash('Please enter your bank name and account name.')
            return redirect('/withdraw')

        if not account_number.isdigit() or not 6 <= len(account_number) <= 20:
            flash('Enter a valid account number.')
            return redirect('/withdraw')

        if not amount.is_finite() or amount < Decimal('300'):
            flash('Minimum withdrawal is ₦300.')
            return redirect('/withdraw')

        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO withdrawals
                        (user_id, bank_name, account_number, account_name, amount, status)
                        VALUES (%s, %s, %s, %s, %s, 'pending')
                    """, (
                        user_id,
                        bank_name,
                        account_number,
                        account_name,
                        amount
                    ))

            flash('Withdrawal request submitted. It is waiting for admin review.')

        except Exception:
            app.logger.exception("Withdrawal request error")
            flash('Could not submit your withdrawal request. Please try again.')

        return redirect('/withdraw')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT username, balance FROM users WHERE id=%s",
                (user_id,)
            )
            user = cur.fetchone()

            cur.execute("""
                SELECT bank_name, account_number, account_name,
                       amount, status, created_at
                FROM withdrawals
                WHERE user_id=%s
                ORDER BY created_at DESC, id DESC
            """, (user_id,))
            history = cur.fetchall()

    return render_template(
        'withdraw.html',
        user=user,
        history=history
    )



@app.route('/deposit', methods=['GET', 'POST'])
def deposit():
    if 'user_id' not in session:
        return redirect('/login')

    user_id = session['user_id']

    if request.method == 'POST':
        sender_name = request.form.get('sender_name', '').strip()

        try:
            amount = Decimal(request.form.get('amount', '0'))
        except InvalidOperation:
            flash('Enter a valid deposit amount.')
            return redirect('/deposit')

        if not sender_name:
            flash('Please enter the sender name.')
            return redirect('/deposit')

        if not amount.is_finite() or amount < Decimal('1000'):
            flash('Minimum deposit is ₦1,000.')
            return redirect('/deposit')

        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO deposits
                        (user_id, sender_name, amount, status)
                        VALUES (%s, %s, %s, 'pending')
                    """, (user_id, sender_name, amount))

            flash('Deposit request submitted. Waiting for admin approval.')

        except Exception:
            app.logger.exception("Deposit request error")
            flash('Could not submit your deposit request.')

        return redirect('/deposit')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT username, balance
                FROM users WHERE id=%s
            """, (user_id,))
            user = cur.fetchone()

            cur.execute("""
                SELECT sender_name, amount, status, created_at
                FROM deposits
                WHERE user_id=%s
                ORDER BY created_at DESC, id DESC
            """, (user_id,))
            history = cur.fetchall()

    return render_template(
        'deposit.html',
        user=user,
        history=history
    )





@app.route('/referrals')
def referrals():
    if 'user_id' not in session:
        return redirect('/login')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT referral_code FROM users WHERE id=%s
            """, (session['user_id'],))
            user = cur.fetchone()

            if not user:
                session.clear()
                return redirect('/login')

            cur.execute("""
                SELECT COALESCE(SUM(amount),0) AS total
                FROM referral_commissions
                WHERE referrer_user_id=%s
            """, (session['user_id'],))
            earnings = cur.fetchone()['total']

            cur.execute("""
                SELECT u.username, rc.amount, rc.created_at, p.name AS plan_name
                FROM referral_commissions rc
                JOIN users u ON u.id=rc.referred_user_id
                LEFT JOIN plans p ON p.id=rc.plan_id
                WHERE rc.referrer_user_id=%s
                ORDER BY rc.created_at DESC
            """, (session['user_id'],))
            commission_history = cur.fetchall()

            cur.execute("""
                SELECT COUNT(*) AS total
                FROM users WHERE referred_by=%s
            """, (session['user_id'],))
            referral_count = cur.fetchone()['total']

    referral_link = request.url_root.rstrip('/') + '/register?ref=' + user['referral_code']

    return render_template(
        'referrals.html',
        referral_link=referral_link,
        referral_code=user['referral_code'],
        earnings=earnings,
        referral_count=referral_count,
        commission_history=commission_history
    )

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


def admin_required():
    return 'admin_id' in session


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, password_hash FROM admin_users
                    WHERE username=%s
                """, (username,))
                admin = cur.fetchone()

        if admin and check_password_hash(admin['password_hash'], password):
            session.clear()
            session['admin_id'] = admin['id']
            return redirect('/admin')

        flash('Incorrect admin username or password.')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect('/admin/login')


@app.route('/admin')
def admin_dashboard():
    if not admin_required():
        return redirect('/admin/login')

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS total FROM users")
            total_users = cur.fetchone()['total']

            cur.execute("""
                SELECT d.id, d.sender_name, d.amount, d.created_at,
                       u.username, u.phone
                FROM deposits d JOIN users u ON u.id=d.user_id
                WHERE d.status='pending' ORDER BY d.id DESC
            """)
            deposits = cur.fetchall()

            cur.execute("""
                SELECT w.id, w.bank_name, w.account_number, w.account_name,
                       w.amount, w.created_at, u.username
                FROM withdrawals w JOIN users u ON u.id=w.user_id
                WHERE w.status='pending' ORDER BY w.id DESC
            """)
            withdrawals = cur.fetchall()

            cur.execute("""
                SELECT s.id, s.submitted_username, s.submitted_at,
                       u.username, t.title, p.name AS plan_name,
                       p.reward_per_task
                FROM task_submissions s
                JOIN users u ON u.id=s.user_id
                JOIN tasks t ON t.id=s.task_id
                LEFT JOIN LATERAL (
                    SELECT p.name, p.reward_per_task
                    FROM user_plans up JOIN plans p ON p.id=up.plan_id
                    WHERE up.user_id=s.user_id AND up.status='active'
                    ORDER BY up.id DESC LIMIT 1
                ) p ON TRUE
                WHERE s.status='pending'
                ORDER BY s.id DESC
            """)
            submissions = cur.fetchall()

            cur.execute("""
                SELECT id, title, task_link, active, created_at
                FROM tasks ORDER BY id DESC LIMIT 30
            """)
            task_list = cur.fetchall()

    return render_template(
        'admin.html',
        total_users=total_users,
        deposits=deposits,
        withdrawals=withdrawals,
        submissions=submissions,
        task_list=task_list
    )


@app.route('/admin/create-task', methods=['POST'])
def admin_create_task():
    if not admin_required():
        return redirect('/admin/login')

    title = request.form.get('title', '').strip()
    task_link = request.form.get('task_link', '').strip()
    description = request.form.get('description', '').strip()

    if not title or not task_link:
        flash('Task title and task link are required.')
        return redirect('/admin')

    if len(title) > 150 or len(task_link) > 2000:
        flash('Task title or link is too long.')
        return redirect('/admin')

    if not task_link.startswith(('https://', 'http://')):
        flash('Enter a valid task link starting with https:// or http://.')
        return redirect('/admin')

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO tasks (title, description, task_link, reward, active)
                    VALUES (%s, %s, %s, 0, TRUE)
                """, (title, description, task_link))
        flash('Task created successfully.')
    except Exception:
        app.logger.exception("Admin task creation failed")
        flash('Could not create the task.')

    return redirect('/admin')


@app.route('/admin/deposit/<int:item_id>/<action>', methods=['POST'])
def admin_deposit_action(item_id, action):
    if not admin_required():
        return redirect('/admin/login')
    if action not in ('approve', 'reject'):
        return redirect('/admin')

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, amount, status
                    FROM deposits WHERE id=%s FOR UPDATE
                """, (item_id,))
                item = cur.fetchone()

                if not item or item[3] != 'pending':
                    flash('This deposit has already been reviewed.')
                    return redirect('/admin')

                if action == 'approve':
                    cur.execute("""
                        UPDATE users SET balance=balance+%s WHERE id=%s
                    """, (item[2], item[1]))
                    cur.execute("""
                        INSERT INTO transactions
                        (user_id, amount, transaction_type, reference)
                        VALUES (%s,%s,'deposit','DEP-' || %s)
                    """, (item[1], item[2], str(item_id)))

                cur.execute("""
                    UPDATE deposits SET status=%s WHERE id=%s
                """, ('approved' if action == 'approve' else 'rejected', item_id))

        flash('Deposit ' + ('approved and balance credited.' if action == 'approve' else 'rejected.'))
    except Exception:
        app.logger.exception("Deposit review failed")
        flash('Could not process deposit.')

    return redirect('/admin')


@app.route('/admin/withdrawal/<int:item_id>/<action>', methods=['POST'])
def admin_withdrawal_action(item_id, action):
    if not admin_required():
        return redirect('/admin/login')
    if action not in ('approve', 'reject'):
        return redirect('/admin')

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, amount, status
                    FROM withdrawals WHERE id=%s FOR UPDATE
                """, (item_id,))
                item = cur.fetchone()

                if not item or item[3] != 'pending':
                    flash('This withdrawal has already been reviewed.')
                    return redirect('/admin')

                if action == 'approve':
                    cur.execute("""
                        UPDATE users SET balance=balance-%s
                        WHERE id=%s AND balance >= %s
                        RETURNING id
                    """, (item[2], item[1], item[2]))

                    if not cur.fetchone():
                        flash('Cannot approve: user has insufficient balance.')
                        return redirect('/admin')

                    cur.execute("""
                        INSERT INTO transactions
                        (user_id, amount, transaction_type, reference)
                        VALUES (%s,%s,'withdrawal','WDR-' || %s)
                    """, (item[1], -item[2], str(item_id)))

                cur.execute("""
                    UPDATE withdrawals SET status=%s WHERE id=%s
                """, ('approved' if action == 'approve' else 'rejected', item_id))

        flash('Withdrawal ' + ('approved. Remember to send the bank payment.' if action == 'approve' else 'rejected.'))
    except Exception:
        app.logger.exception("Withdrawal review failed")
        flash('Could not process withdrawal.')

    return redirect('/admin')


@app.route('/admin/submission/<int:item_id>/<action>', methods=['POST'])
def admin_submission_action(item_id, action):
    if not admin_required():
        return redirect('/admin/login')
    if action not in ('approve', 'reject'):
        return redirect('/admin')

    try:
        with db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.id, s.user_id, s.status,
                           p.reward_per_task
                    FROM task_submissions s
                    LEFT JOIN LATERAL (
                        SELECT p.reward_per_task
                        FROM user_plans up JOIN plans p ON p.id=up.plan_id
                        WHERE up.user_id=s.user_id AND up.status='active'
                        ORDER BY up.id DESC LIMIT 1
                    ) p ON TRUE
                    WHERE s.id=%s FOR UPDATE OF s
                """, (item_id,))
                item = cur.fetchone()

                if not item or item['status'] != 'pending':
                    flash('This submission has already been reviewed.')
                    return redirect('/admin')

                if action == 'approve':
                    if item['reward_per_task'] is None:
                        flash('Cannot approve: user has no active plan.')
                        return redirect('/admin')

                    cur.execute("""
                        UPDATE users SET balance=balance+%s WHERE id=%s
                    """, (item['reward_per_task'], item['user_id']))

                    cur.execute("""
                        INSERT INTO transactions
                        (user_id, amount, transaction_type, reference)
                        VALUES (%s,%s,'task_reward',%s)
                    """, (
                        item['user_id'],
                        item['reward_per_task'],
                        'TASK-' + str(item_id)
                    ))

                cur.execute("""
                    UPDATE task_submissions SET status=%s WHERE id=%s
                """, ('approved' if action == 'approve' else 'rejected', item_id))

        flash('Submission ' + ('approved and reward credited.' if action == 'approve' else 'rejected without reward.'))
    except Exception:
        app.logger.exception("Task submission review failed")
        flash('Could not process submission.')

    return redirect('/admin')



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
