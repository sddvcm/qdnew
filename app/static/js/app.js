function showToast(type, message) {
  const toast = document.getElementById('toast');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  toast.style.display = 'block';
  setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

function togglePassword(input) {
  if (input.type === 'password') {
    input.type = 'text';
  } else {
    input.type = 'password';
  }
}

function togglePasswordBtn(btn) {
  const wrapper = btn.closest('.password-wrapper');
  if (!wrapper) return;
  const input = wrapper.querySelector('input');
  if (!input) return;
  togglePassword(input);
}

// ==================== 全局鉴权拦截 ====================
// 为什么需要：会话过期 / 密码被改后，页面上的 fetch 会拿到 401。
// 如果不统一处理，各个页面只会弹出「读取失败：Unexpected token '<'」
// 之类毫无意义的报错（因为 401 响应体不是 JSON 或 JSON 里没有预期字段）。
// 这里包一层 fetch，遇到 401 就直接跳登录页，不打断原调用方的写法。
(function () {
  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    return _fetch(input, init).then(resp => {
      if (resp.status === 401) {
        // 已经在登录页就别跳了，否则会刷新循环
        if (!/^\/login\b/.test(location.pathname)) {
          const nxt = location.pathname + location.search;
          location.href = '/login?next=' + encodeURIComponent(nxt);
        }
      }
      return resp;
    });
  };
})();

// 顶栏进出：已登录时把「退出登录」按钮显示出来
document.addEventListener('DOMContentLoaded', function () {
  const out = document.getElementById('logoutBtn');
  if (!out) return;
  out.addEventListener('click', async function () {
    if (!confirm('确定退出登录？')) return;
    try { await fetch('/api/auth/logout', {method: 'POST'}); } catch (e) {}
    location.href = '/login';
  });
});
