// Aplica o tema salvo antes da primeira pintura (carregado sem defer no <head>) e marca que o JS está ativo.
(function () {
  var r = document.documentElement;
  r.classList.add('js');
  try {
    var t = localStorage.getItem('tema');
    if (t === 'claro' || t === 'escuro') r.setAttribute('data-tema', t);
  } catch (e) { /* armazenamento bloqueado: segue o tema do sistema */ }
})();
