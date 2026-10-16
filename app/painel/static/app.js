// Comportamentos do painel. Fica em arquivo próprio porque a CSP (script-src 'self') bloqueia handlers inline.
document.addEventListener('submit', function (e) {
  var f = e.target;
  if (!f || !f.getAttribute) return;
  var msg = f.getAttribute('data-confirmar');
  if (msg && !window.confirm(msg)) {
    e.preventDefault();
    return;
  }
  // Indicador de carregamento: o botão que enviou o formulário mostra um spinner até a página responder.
  // Não usa `disabled` porque isso tiraria o name/value do botão (ex.: "Remover") do envio.
  if (e.defaultPrevented || f.target === '_blank' || f.tagName !== 'FORM') return;
  if (f.hasAttribute('data-enviando')) { e.preventDefault(); return; }  // evita enviar duas vezes
  f.setAttribute('data-enviando', '');
  var botao = e.submitter || f.querySelector('button:not([type="button"])');
  if (botao) {
    botao.classList.add('carregando');
    botao.setAttribute('aria-busy', 'true');
  }
});

// Voltar pelo histórico pode restaurar a página do cache já com o spinner ligado: limpa.
window.addEventListener('pageshow', function (e) {
  if (!e.persisted) return;
  document.querySelectorAll('form[data-enviando]').forEach(function (f) { f.removeAttribute('data-enviando'); });
  document.querySelectorAll('button.carregando').forEach(function (b) {
    b.classList.remove('carregando');
    b.removeAttribute('aria-busy');
  });
});

// Botões que chamam o servidor via HTMX (ex.: "Melhorar com a LLM", "Testar", "Descartar"):
// o HTMX põe a classe htmx-request no botão durante a chamada; aqui também sinalizamos aria-busy.
document.addEventListener('htmx:beforeRequest', function (e) {
  var el = e.detail && e.detail.elt;
  if (el && el.tagName === 'BUTTON') el.setAttribute('aria-busy', 'true');
});
document.addEventListener('htmx:afterRequest', function (e) {
  var el = e.detail && e.detail.elt;
  if (el && el.removeAttribute) el.removeAttribute('aria-busy');
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
