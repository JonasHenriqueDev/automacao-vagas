// Comportamentos do painel. Fica em arquivo próprio porque a CSP (script-src 'self') bloqueia handlers inline.
document.addEventListener('submit', function (e) {
  var f = e.target;
  if (!f || !f.getAttribute) return;
  var msg = f.getAttribute('data-confirmar');
  if (msg && !window.confirm(msg)) {
    e.preventDefault();
    return;
  }
  // Formulários que abrem em nova aba (ex.: Aplicar): recarrega esta página depois, para refletir o novo estado.
  if (f.hasAttribute('data-recarregar')) {
    setTimeout(function () { window.location.reload(); }, 900);
  }
});
