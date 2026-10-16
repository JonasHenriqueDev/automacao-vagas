// Comportamentos do painel. Fica em arquivo próprio porque a CSP (script-src 'self') bloqueia handlers inline.
document.addEventListener('submit', function (e) {
  var f = e.target;
  if (!f || !f.getAttribute) return;
  var msg = f.getAttribute('data-confirmar');
  if (msg && !window.confirm(msg)) {
    e.preventDefault();
    return;
  }
});

// Tema claro/escuro: o botão alterna entre os dois e guarda a escolha; sem escolha, vale o tema do sistema.
(function () {
  var raiz = document.documentElement;
  var botao = document.getElementById('alternar-tema');
  var escuroSistema = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function escuroAtivo() {
    var t = raiz.getAttribute('data-tema');
    if (t) return t === 'escuro';
    return !!(escuroSistema && escuroSistema.matches);
  }
  function atualizarBotao() {
    if (!botao) return;
    var escuro = escuroAtivo();
    botao.setAttribute('aria-pressed', escuro ? 'true' : 'false');
    botao.setAttribute('aria-label', escuro ? 'Mudar para o tema claro' : 'Mudar para o tema escuro');
    botao.setAttribute('title', escuro ? 'Tema claro' : 'Tema escuro');
  }
  if (botao) {
    botao.addEventListener('click', function () {
      var novo = escuroAtivo() ? 'claro' : 'escuro';
      raiz.setAttribute('data-tema', novo);
      try { localStorage.setItem('tema', novo); } catch (e) { /* ignora */ }
      atualizarBotao();
    });
  }
  if (escuroSistema && escuroSistema.addEventListener) escuroSistema.addEventListener('change', atualizarBotao);
  atualizarBotao();
})();

// Menu lateral: gaveta deslizante em telas estreitas (fixa em telas largas).
(function () {
  var raiz = document.documentElement;
  var abrir = document.getElementById('abrir-menu');
  var gaveta = document.getElementById('menu');
  var cortina = document.getElementById('cortina');
  if (!abrir || !gaveta) return;
  var larga = window.matchMedia('(min-width: 900px)');

  function definir(aberto) {
    raiz.classList.toggle('menu-aberto', aberto);
    abrir.setAttribute('aria-expanded', aberto ? 'true' : 'false');
    if (aberto) {
      var primeiro = gaveta.querySelector('a');
      if (primeiro) primeiro.focus();
    }
  }
  abrir.addEventListener('click', function () { definir(!raiz.classList.contains('menu-aberto')); });
  if (cortina) cortina.addEventListener('click', function () { definir(false); abrir.focus(); });
  gaveta.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('a')) definir(false); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && raiz.classList.contains('menu-aberto')) { definir(false); abrir.focus(); }
  });
  function aoMudarLargura() { if (larga.matches) definir(false); }
  if (larga.addEventListener) larga.addEventListener('change', aoMudarLargura);
})();
