/* Same-origin browser client: all authoritative business state comes from SQLite. */
'use strict';

const el = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[character]));
const yuan = (cents) => `¥${(Number(cents || 0) / 100).toFixed(2)}`;
const dateTime = (seconds) => seconds
  ? new Date(seconds * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}) : '—';
const names = {
  pending_payment: '待支付', pending_acceptance: '待接单', preparing: '制作中',
  ready: '待取餐', delivering: '配送中', delivered: '已送达', completed: '已完成',
  cancelled: '已取消', closed: '已关闭', open: '可抢单', offered: '待响应',
  accepted: '已承接', pending: '待审核', approved: '已通过', rejected: '已驳回',
  processing: '退款处理中', refunded: '已退款', failed: '退款失败',
  customer: '顾客', merchant: '商家', rider: '骑手', admin: '管理员'
};
let profile = null;
let csrf = '';
let scale = 1;
let page = '';
let busy = false;
let modalAction = null;
let modalClose = null;
let positionTimer = null;
let authMode = 'password';
let administrator = false;
let registering = false;
let cache = {};

async function api(path, method = 'GET', data = null, requestKey = null) {
  const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
  if (method !== 'GET') headers['Idempotency-Key'] = requestKey || crypto.randomUUID();
  const response = await fetch(`/api${path}`, {
    method, headers, credentials: 'same-origin',
    ...(data === null ? {} : {body: JSON.stringify(data)})
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || '请求失败');
    error.code = result.code;
    error.details = result.details;
    throw error;
  }
  return result;
}

function notify(message) {
  el('#toast').innerHTML = `<div class="toast">${escapeHtml(message)}</div>`;
  setTimeout(() => { el('#toast').innerHTML = ''; }, 3500);
}

function closeDialog() {
  if (positionTimer) clearInterval(positionTimer);
  positionTimer = null;
  el('#modal').innerHTML = '';
  modalAction = null;
  if (modalClose) modalClose();
  modalClose = null;
}

function field(name, label, value = '', type = 'text', options = null) {
  const id = `f-${name}`;
  let control;
  if (options) {
    control = `<select id="${id}" name="${name}">${options.map(([v, t]) =>
      `<option value="${escapeHtml(v)}" ${String(v) === String(value) ? 'selected' : ''}>`
      + `${escapeHtml(t)}</option>`).join('')}</select>`;
  } else if (type === 'textarea') {
    control = `<textarea id="${id}" name="${name}" rows="3">${escapeHtml(value)}</textarea>`;
  } else {
    control = `<input id="${id}" name="${name}" type="${type}" value="${escapeHtml(value)}"`
      + (type === 'number' ? ' step="any"' : '') + '>';
  }
  return `<label for="${id}">${escapeHtml(label)}</label>${control}`;
}

function dialog(title, content, callback = null, submitLabel = '确认') {
  closeDialog();
  modalAction = callback;
  el('#modal').innerHTML = `<div class="modalback"><section class="modal" role="dialog" aria-modal="true">`
    + `<div class="between"><h2>${escapeHtml(title)}</h2>`
    + '<button class="close" data-action="close" aria-label="关闭">✕</button></div>'
    + `<form id="modal-form">${content}<div class="error" id="modal-error"></div>`
    + (callback ? `<div class="actions"><button type="submit">${escapeHtml(submitLabel)}</button></div>` : '')
    + '</form></section></div>';
}

function button(label, action, id = '', extra = '', secondary = true) {
  return `<button class="${secondary ? 'secondary' : ''}" data-action="${action}"`
    + ` data-id="${escapeHtml(id)}" ${extra}>${escapeHtml(label)}</button>`;
}

function badge(value) {
  return `<span class="badge">${escapeHtml(names[value] || value)}</span>`;
}

function table(headers, rows) {
  return `<div class="card tablewrap"><table><thead><tr>${headers.map((h) =>
    `<th>${escapeHtml(h)}</th>`).join('')}</tr></thead><tbody>`
    + (rows.length ? rows.map((r) => `<tr>${r.map((cell) => `<td>${cell}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${headers.length}">暂无匹配数据</td></tr>`)
    + '</tbody></table></div>';
}

function heading(title, subtitle = '') {
  return `<div class="head"><h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p></div>`;
}

async function bootstrap() {
  const session = await api('/session');
  profile = session.user;
  csrf = session.csrf;
  scale = session.time_scale;
  page = profile ? ({customer: 'home', merchant: 'orders', rider: 'tasks', admin: 'users'}[profile.role]) : '';
  await render();
}

function authPage() {
  const fields = registering
    ? field('role', '注册身份', 'customer', 'text', [['customer', '顾客'], ['rider', '骑手'], ['merchant', '商家']])
      + field('account', '账号') + field('name', '名称') + field('phone', '手机号')
      + field('password', '密码，至少8位', '', 'password') + field('confirm', '确认密码', '', 'password')
    : authMode === 'password'
      ? field('account', '账号') + field('password', '密码', '', 'password')
      : field('phone', '手机号') + button('获取模拟验证码', 'sms') + field('code', '验证码');
  el('#app').innerHTML = `<div class="auth"><section class="authart"><div class="brand">食间</div>`
    + '<div><h1>把时间留给生活，<br>把好饭送到身边。</h1><p>持久化订单 · 独立角色空间</p>'
    + '<div class="plate">🍱</div></div><small>实验三 · 支付、短信和定位均为模拟</small></section>'
    + `<section class="authform"><div class="form"><div class="brand">食间</div><h1>`
    + `${registering ? '创建账号' : administrator ? '管理员登录' : '欢迎回来'}</h1>`
    + (!registering ? `<div class="tabs">${button('账号密码登录', 'password')}`
      + (!administrator ? button('手机号验证登录', 'phone') : '') + '</div>' : '')
    + `<form id="auth-form">${fields}<div class="error" id="auth-error"></div>`
    + `<button class="full">${registering ? '注册' : '登录'}</button></form><div class="actions">`
    + button(registering ? '返回登录' : '没有账号？注册', 'register')
    + button(administrator ? '返回普通用户登录' : '管理员身份登录', 'admin-entry')
    + '</div><div class="demo">演示账号：customer / merchant / rider / admin<br>'
    + '演示密码：Demo12345!<br>验证码通过页面模拟返回，不发送真实短信。<br>'
    + '新商家和骑手须平台审核后开展业务。</div></div></section></div>';
}

const navigation = {
  customer: [['点餐', 'home'], ['购物车', 'cart'], ['订单', 'orders'], ['地址', 'addresses']],
  merchant: [['订单处理', 'orders'], ['菜品库存', 'dishes'], ['店铺设置', 'shops'], ['营收统计', 'stats']],
  rider: [['任务大厅', 'tasks'], ['配送订单', 'orders']],
  admin: [['主体审核', 'users'], ['平台订单', 'orders'], ['配送异常', 'exceptions'],
    ['退款处理', 'refunds'], ['店铺管理', 'shops'], ['经营统计', 'stats'], ['操作日志', 'logs']]
};

async function render() {
  if (!profile) return authPage();
  let html = '';
  if (profile.approval !== 'approved') {
    html = heading('入驻申请审核中', `审核状态：${names[profile.approval]}`)
      + '<p>可退出后使用管理员演示账号处理审核，再重新登录。</p>';
  } else {
    html = await pages[page]();
  }
  el('#app').innerHTML = `<header class="top"><div class="row"><span class="brand">食间</span>`
    + `<span class="role">${names[profile.role]}端</span></div><div class="row">`
    + `<span>${escapeHtml(profile.name)}</span>${button('退出登录', 'logout')}</div></header>`
    + `<div class="backend-banner">SQLite持久化 · 支付/短信/位置模拟 · 时间倍率 ${scale}`
    + (profile.enabled ? '' : ' · 账号已停用，仅保留历史履约') + '</div>'
    + `<div class="workspace"><aside>${navigation[profile.role].map(([label, id]) =>
      button(label, 'navigate', id, `style="${id === page ? 'background:#edf3df' : ''}"`)).join('')}`
    + (profile.role === 'admin' ? button('立即扫描超时任务', 'scan') : '')
    + `</aside><main class="content">${html}</main></div>`;
}

const pages = {
  async home() {
    const query = cache.dishQuery || '';
    cache.dishes = await api(`/dishes${query}`);
    return heading('认真吃饭，从这一餐开始', '菜品查询与单店下单，配送费每单5元')
      + '<form id="dish-filter" class="filters"><input name="keyword" placeholder="菜品关键词">'
      + '<input name="category" placeholder="分类"><input name="max_price" type="number" placeholder="最高价格 / 元">'
      + '<button>查询</button></form><div class="grid">'
      + cache.dishes.map((d) => `<article class="card food"><div class="foodpic">🍱</div>`
        + `<h3>${escapeHtml(d.name)}</h3><p>${escapeHtml(d.shop_name)} · ${escapeHtml(d.category)}</p>`
        + `<div class="between"><span class="price">${yuan(d.price)}</span>`
        + button(d.stock ? '加入购物车' : '售罄', 'add-cart', d.id, d.stock ? '' : 'disabled', false)
        + '</div></article>').join('') + '</div>';
  },
  async cart() {
    cache.cart = await api('/cart');
    cache.addresses = await api('/addresses');
    const shops = [...new Set(cache.cart.map((c) => c.shop_id))];
    return heading('购物车', '每次只结算一家店；价格变化需确认')
      + table(['菜品', '单价', '数量', '操作'], cache.cart.map((c) => [escapeHtml(c.name),
        yuan(c.price), c.quantity, button('修改数量', 'quantity', c.dish_id)
        + ' ' + button('移除', 'remove-cart', c.dish_id)]))
      + `<div class="actions">${shops.map((id) => button(`结算 ${cache.cart.find((c) =>
        c.shop_id === id).shop_name}`, 'checkout', id, '', false)).join('')}</div>`;
  },
  async addresses() {
    cache.addresses = await api('/addresses');
    return heading('收货地址', '订单保存地址快照，付款后不能更换收货地址')
      + button('新增地址', 'address-create', '', '', false)
      + table(['区域', '详细地址', '联系人', '手机号', '操作'], cache.addresses.map((a) =>
        [a.region, a.detail, a.contact, a.phone].map(escapeHtml).concat(button('编辑', 'address-edit', a.id))));
  },
  async orders() {
    cache.orders = await api(`/orders${cache.orderQuery || ''}`);
    return heading('订单', '按创建时间、状态及实付金额组合查询')
      + '<form id="order-filter" class="filters"><input name="id" placeholder="订单编号">'
      + '<select name="status"><option value="">全部状态</option>'
      + Object.entries(names).filter(([id]) => ['pending_payment', 'pending_acceptance', 'preparing',
        'ready', 'delivering', 'delivered', 'completed', 'cancelled', 'closed'].includes(id))
        .map(([id, name]) => `<option value="${id}">${name}</option>`).join('') + '</select>'
      + '<input name="min_amount" placeholder="最低实付 / 元"><input name="max_amount" placeholder="最高实付 / 元">'
      + '<input name="from" type="datetime-local" aria-label="创建时间开始">'
      + '<input name="to" type="datetime-local" aria-label="创建时间结束"><button>查询</button></form>'
      + table(['订单号', '创建时间', '实付', '状态', '操作'], cache.orders.map((o) => [
        o.id, dateTime(o.created_at), yuan(o.total), badge(o.status), orderButtons(o)
      ]));
  },
  async dishes() {
    cache.dishes = await api('/dishes');
    return heading('菜品与库存', '编辑时检查版本，避免旧页面覆盖他人的修改')
      + button('新增菜品', 'dish-create', '', '', false)
      + table(['菜品', '分类', '价格', '库存', '上架', '操作'], cache.dishes.map((d) => [
        escapeHtml(d.name), escapeHtml(d.category), yuan(d.price), d.stock,
        d.on_sale ? '已上架' : '已下架', button('编辑', 'dish-edit', d.id)
        + ' ' + button('删除', 'dish-delete', d.id)
      ]));
  },
  async shops() {
    cache.shops = await api('/shops');
    return heading('店铺管理', '停业或停用不自动撤销已有有效订单')
      + table(['店铺', '区域', '营业', '启用', '操作'], cache.shops.map((s) => [
        escapeHtml(s.name), escapeHtml(s.region), s.is_open ? '营业中' : '打烊',
        s.enabled ? '启用' : '停用', button('设置', 'shop-edit', s.id)
      ]));
  },
  async tasks() {
    cache.tasks = await api('/tasks');
    return heading('配送任务', `当前占用 ${cache.tasks.occupied}/3；派单待响应也占名额`)
      + button(profile.online ? '下线' : '上线', 'online', '', '', false)
      + table(['任务', '订单', '状态', '承接骑手', '操作'], cache.tasks.items.map((t) => [
        t.id, t.order_id, badge(t.status), t.rider_id || '未分配', taskButtons(t)
      ]));
  },
  async users() {
    cache.users = await api('/users');
    return heading('主体审核', '商家和骑手审核后开展业务，停用保留历史履约')
      + table(['账号', '身份', '审核', '启用', '操作'], cache.users.map((u) => [
        escapeHtml(u.account), names[u.role], badge(u.approval), u.enabled ? '启用' : '停用',
        u.role === 'admin' ? '管理员' : (['merchant', 'rider'].includes(u.role)
          ? button('通过', 'approve', u.id) + ' ' + button('驳回', 'reject-user', u.id) + ' ' : '')
          + button(u.enabled ? '停用' : '启用', 'toggle-user', u.id)
      ]));
  },
  async exceptions() {
    cache.exceptions = await api('/exceptions');
    return heading('异常协调', '终止订单与退款结果分开保存')
      + table(['编号', '订单', '原因', '处理结果', '操作'], cache.exceptions.map((x) => [
        x.id, x.order_id, escapeHtml(x.reason), escapeHtml(x.status),
        x.status === 'pending' ? button('处理', 'resolve', x.id) : escapeHtml(x.resolution)
      ]));
  },
  async refunds() {
    cache.refunds = await api('/refunds');
    return heading('退款处理', '退款失败保留余额并允许重试，不恢复订单配送')
      + table(['编号', '订单', '菜品退款', '配送退款', '状态', '次数', '操作'], cache.refunds.map((r) => [
        r.id, r.order_id, yuan(r.food_amount), yuan(r.delivery_amount), badge(r.status), r.attempts,
        r.status === 'refunded' ? '已完成' : button('模拟成功', 'refund-success', r.id)
          + ' ' + button('模拟失败', 'refund-failure', r.id)
      ]));
  },
  async stats() {
    const data = await api(`/statistics${cache.statsQuery || ''}`);
    return heading('经营统计', '上海时间，按完成日期归属；仅扣除成功退款')
      + '<form id="stats-filter" class="actions"><select name="period"><option value="day">按日</option>'
      + '<option value="month">按月</option></select><button>查询</button></form>'
      + table(['周期', '完成单量', '菜品净收入', '配送费净额'], data.items.map((s) => [
        s.period, s.completed_orders, yuan(s.food_net), yuan(s.delivery_net)
      ]));
  },
  async logs() {
    const data = await api(`/logs${cache.logQuery || ''}`);
    return heading('操作日志', '只读审计，失败和拒绝操作也保留记录')
      + '<form id="log-filter" class="filters"><input name="actor_id" placeholder="操作人编号">'
      + '<input name="order_id" placeholder="关联订单编号"><input name="action" placeholder="操作类型">'
      + '<input name="from" type="datetime-local" aria-label="开始时间">'
      + '<input name="to" type="datetime-local" aria-label="结束时间"><button>查询</button></form>'
      + table(['时间', '操作人', '操作', '对象', '结果'], data.map((l) => [
        dateTime(l.created_at), escapeHtml(l.actor || '系统'), escapeHtml(l.action),
        escapeHtml(`${l.object_type}:${l.object_id}`), escapeHtml(l.result)
      ]));
  }
};

function orderButtons(order) {
  let result = button('详情', 'order-detail', order.id);
  const choices = profile.role === 'customer'
    ? ({pending_payment: ['pay', 'cancel'], pending_acceptance: ['cancel'], delivered: ['complete']})
    : profile.role === 'merchant' ? {pending_acceptance: ['accept', 'reject'], preparing: ['ready']} : {};
  const labels = {pay: '模拟支付', cancel: '取消', complete: '确认收餐', accept: '接单', reject: '拒单', ready: '出餐'};
  for (const action of choices[order.status] || []) {
    result += ' ' + button(labels[action], `order-${action}`, order.id);
  }
  if (['customer', 'rider'].includes(profile.role) && ['preparing', 'ready', 'delivering'].includes(order.status)) {
    result += ' ' + button('异常上报', 'report', order.id);
  }
  if (order.status === 'delivering' && profile.role !== 'merchant') {
    result += ' ' + button('模拟位置', 'position', order.id);
  }
  if (profile.role === 'admin') result += ' ' + button('申请退款', 'refund-create', order.id);
  return result;
}

function taskButtons(task) {
  if (!task.mine) return task.status === 'open' && !task.escalated
    ? button('抢单', 'task-claim', task.id) : '平台协调中';
  const actions = {offered: ['accept', 'reject'], accepted: ['pickup', 'abandon'], delivering: ['deliver']};
  const labels = {accept: '接受派单', reject: '拒绝', pickup: '确认取餐', abandon: '放弃任务', deliver: '标记送达'};
  return (actions[task.status] || []).map((a) => button(labels[a], `task-${a}`, task.id)).join(' ');
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function mutation(path, data, method = 'POST') {
  const result = await api(path, method, data);
  await render();
  return result;
}

document.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  try {
    const form = event.target;
    const data = formData(form);
    if (form.id === 'auth-form') {
      if (registering) {
        if (data.password !== data.confirm) throw new Error('两次密码不一致');
        await api('/register', 'POST', data);
        registering = false;
        authPage();
        notify('注册成功，请登录；商家和骑手需审核');
      } else {
        const result = await api('/login', 'POST', {...data, mode: authMode, admin: administrator});
        profile = result.user;
        csrf = result.csrf;
        page = navigation[profile.role][0][1];
        await render();
      }
    } else if (form.id === 'modal-form' && modalAction) {
      await modalAction(data);
      closeDialog();
      await render();
    } else {
      const config = {'dish-filter': 'dishQuery', 'order-filter': 'orderQuery',
        'log-filter': 'logQuery', 'stats-filter': 'statsQuery'};
      if (config[form.id]) {
        for (const key of ['from', 'to']) {
          if (data[key]) data[key] = Math.floor(new Date(data[key]).getTime() / 1000);
        }
        cache[config[form.id]] = '?' + new URLSearchParams(Object.entries(data).filter(([, v]) => v !== ''));
        await render();
      }
    }
  } catch (error) {
    const target = el('#modal-error') || el('#auth-error');
    if (target) target.textContent = error.message;
    else notify(error.message);
  } finally {
    busy = false;
  }
});

document.addEventListener('click', async (event) => {
  const target = event.target.closest('button[data-action]');
  if (!target || target.disabled || busy) return;
  busy = true;
  try {
    await handleAction(target.dataset.action, Number(target.dataset.id) || target.dataset.id);
  } catch (error) {
    notify(error.message);
  } finally {
    busy = false;
  }
});

async function handleAction(action, id) {
  if (action === 'close') return closeDialog();
  if (action === 'navigate') { page = id; return render(); }
  if (action === 'password' || action === 'phone') { authMode = action; return authPage(); }
  if (action === 'register') { registering = !registering; return authPage(); }
  if (action === 'admin-entry') { administrator = !administrator; registering = false; authMode = 'password'; return authPage(); }
  if (action === 'logout') { await api('/logout', 'POST', {}); return bootstrap(); }
  if (action === 'sms') {
    const result = await api('/sms', 'POST', {phone: el('#f-phone').value});
    return notify(`模拟验证码：${result.demo_code}，5分钟内有效`);
  }
  if (action === 'add-cart') {
    const rows = await api('/cart');
    const old = rows.find((r) => r.dish_id === id);
    await api('/cart', 'PUT', {dish_id: id, quantity: (old?.quantity || 0) + 1});
    return notify('已加入购物车');
  }
  if (action === 'remove-cart') return mutation('/cart', {dish_id: id, quantity: 0}, 'PUT');
  if (action === 'quantity') {
    const item = cache.cart.find((r) => r.dish_id === id);
    return dialog('修改数量', field('quantity', '份数，零表示移除', item.quantity, 'number'),
      (data) => api('/cart', 'PUT', {dish_id: id, quantity: Number(data.quantity)}));
  }
  if (action === 'checkout') {
    const items = cache.cart.filter((r) => r.shop_id === id);
    const changes = items.filter((r) => r.price !== r.seen_price);
    const total = items.reduce((sum, r) => sum + r.price * r.quantity, 500);
    return dialog('确认订单', `<p>实付 ${yuan(total)}，含配送费¥5.00</p>`
      + (changes.length ? '<div class="notice">价格已变化，提交即确认下方最新单价：'
        + changes.map((r) => `${escapeHtml(r.name)} ${yuan(r.price)}`).join('；') + '</div>' : '')
      + field('address_id', '收货地址', '', 'text', cache.addresses.map((a) =>
        [a.id, `${a.region} ${a.detail}`])), async (data) => {
      await api('/orders', 'POST', {shop_id: id, address_id: Number(data.address_id),
        confirmed_prices: Object.fromEntries(items.map((r) => [String(r.dish_id), r.price]))});
      page = 'orders';
    }, '确认价格并创建订单');
  }
  if (action.startsWith('address-')) {
    const address = cache.addresses?.find((a) => a.id === id) || {};
    return dialog('收货地址', field('region', '校区', address.region || '仙林校区')
      + field('detail', '详细地址', address.detail) + field('contact', '联系人', address.contact)
      + field('phone', '手机号', address.phone), (data) => api(
      address.id ? `/addresses/${id}` : '/addresses', address.id ? 'PUT' : 'POST', data));
  }
  if (action === 'order-detail') {
    const o = await api(`/orders/${id}`);
    return dialog('订单详情', `${badge(o.status)}<p>实付 ${yuan(o.total)}</p>`
      + o.items.map((i) => `<p>${escapeHtml(i.name)} × ${i.quantity} · ${yuan(i.unit_price)}</p>`).join('')
      + `<p>${escapeHtml(o.address.region)} ${escapeHtml(o.address.detail)}</p>`
      + '<h3>退款记录</h3>' + o.refunds.map((r) => `<p>${badge(r.status)} 菜品${yuan(r.food_amount)}`
        + ` / 配送${yuan(r.delivery_amount)}</p>`).join(''));
  }
  if (action.startsWith('order-')) {
    const cmd = action.slice(6);
    const order = await api(`/orders/${id}`);
    const content = cmd === 'reject' ? field('reason', '拒单理由', '食材售罄', 'text',
      ['食材售罄', '设备故障', '门店打烊'].map((v) => [v, v])) : cmd === 'pay'
      ? field('outcome', '模拟支付结果', 'success', 'text', [['success', '成功'], ['failure', '失败']])
      : `<p>确认执行此操作？当前状态：${escapeHtml(names[order.status])}</p>`;
    return dialog('确认订单操作', content, (data) => api(`/orders/${id}/actions`, 'POST',
      {...data, action: cmd, version: order.version}));
  }
  if (action === 'online') {
    profile = await mutation('/rider/online', {online: !profile.online});
    return render();
  }
  if (action.startsWith('task-')) {
    const cmd = action.slice(5);
    return dialog('配送操作', cmd === 'abandon' ? field('reason', '放弃原因') : '<p>确认执行此配送操作？</p>',
      (data) => api(`/tasks/${id}/actions`, 'POST', {...data, action: cmd}));
  }
  if (action === 'report') return dialog('上报订单异常', field('reason', '原因', '', 'textarea'),
    (data) => api(`/orders/${id}/exceptions`, 'POST', data));
  if (action === 'position') {
    const show = async () => {
      const p = await api(`/orders/${id}/position`);
      const output = el('#position');
      if (output) output.textContent = `模拟坐标：${p.x},${p.y}；刷新批次：${p.tick}`;
    };
    dialog('配送模拟位置', '<div class="route"><p id="position">正在查询…</p></div>'
      + '<p>每10秒重新请求后端；送达后关闭位置访问。</p>');
    await show();
    positionTimer = setInterval(() => show().catch((error) => { closeDialog(); notify(error.message); }), 10000);
    return;
  }
  if (action === 'dish-delete') return dialog('删除菜品', '<p>历史订单快照仍保留。</p>',
    () => api(`/dishes/${id}`, 'DELETE', {}));
  if (action === 'dish-create' || action === 'dish-edit') {
    const d = cache.dishes.find((dish) => dish.id === id) || {};
    return dialog('菜品信息', field('name', '名称', d.name) + field('category', '分类', d.category || '盖饭')
      + field('price', '价格 / 元', d.price ? d.price / 100 : '', 'number')
      + field('stock', '库存', d.stock || 0, 'number')
      + field('on_sale', '是否上架', d.on_sale === 0 ? 'false' : 'true', 'text', [['true', '上架'], ['false', '下架']]),
    (data) => api(d.id ? `/dishes/${id}` : '/dishes', d.id ? 'PUT' : 'POST',
      {...data, stock: Number(data.stock), on_sale: data.on_sale === 'true', version: d.version}));
  }
  if (action === 'shop-edit') {
    const s = cache.shops.find((shop) => shop.id === id);
    return dialog('店铺设置', field('name', '名称', s.name) + field('description', '简介', s.description)
      + field('region', '配送区域', s.region)
      + field('is_open', '营业状态', String(Boolean(s.is_open)), 'text', [['true', '营业'], ['false', '打烊']])
      + (profile.role === 'admin' ? field('enabled', '店铺启用', String(Boolean(s.enabled)), 'text',
        [['true', '启用'], ['false', '停用']]) : ''), (data) => api(`/shops/${id}`, 'PATCH',
      {...data, is_open: data.is_open === 'true', ...(data.enabled ? {enabled: data.enabled === 'true'} : {})}));
  }
  if (['approve', 'reject-user', 'toggle-user'].includes(action)) {
    const u = cache.users.find((item) => item.id === id);
    const data = action === 'toggle-user' ? {enabled: !u.enabled}
      : {approval: action === 'approve' ? 'approved' : 'rejected'};
    return mutation(`/users/${id}`, data, 'PATCH');
  }
  if (action === 'resolve') return dialog('平台处理异常', field('reason', '处理说明', '', 'textarea')
    + field('decision', '决定', 'continue', 'text', [['continue', '继续配送'], ['terminate', '终止订单']])
    + field('food_amount', '菜品退款 / 元', 0, 'number')
    + field('delivery_amount', '配送费退款 / 元', 0, 'number'),
  (data) => api(`/exceptions/${id}/resolve`, 'POST', data));
  if (action.startsWith('refund-') && action !== 'refund-create') {
    return mutation(`/refunds/${id}/process`, {outcome: action.endsWith('success') ? 'success' : 'failure'});
  }
  if (action === 'refund-create') return dialog('申请部分退款', field('reason', '退款原因')
    + field('food_amount', '菜品退款 / 元', 0, 'number')
    + field('delivery_amount', '配送费退款 / 元', 0, 'number'),
  (data) => api(`/orders/${id}/refunds`, 'POST', data));
  if (action === 'scan') {
    await mutation('/jobs/scan', {});
    return notify('超时扫描完成');
  }
}

bootstrap().catch((error) => {
  el('#app').innerHTML = `<div class="empty">连接失败：${escapeHtml(error.message)}</div>`;
});
