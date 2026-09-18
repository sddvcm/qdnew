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
