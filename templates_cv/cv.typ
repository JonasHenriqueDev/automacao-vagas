// CV compatível com ATS: uma coluna, sem tabelas nem imagens, fontes padrão.
// Os dados vêm de dados.json (strings inseridas como texto, nunca avaliadas como código).
#let d = json("dados.json")

#set document(title: d.nome + " - " + d.titulo, author: d.nome)
#set page(paper: "a4", margin: (x: 1.9cm, y: 1.6cm))
#set text(font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"), size: 10pt, lang: d.idioma)
#set par(justify: false, leading: 0.55em, spacing: 0.7em)
#set list(indent: 0.4em, body-indent: 0.5em, spacing: 0.45em)

// Hiperlinks clicáveis, mas com a aparência do texto ao redor (sem azul, sem sublinhado).
#show link: set text(fill: black)
#let contato(c) = if c.url != "" { link(c.url)[#c.texto] } else { [#c.texto] }

#let secao(titulo) = {
  v(0.7em)
  text(size: 11pt, weight: "bold")[#upper(titulo)]
  v(-0.5em)
  line(length: 100%, stroke: 0.6pt)
  v(0.1em)
}

#align(left)[
  #text(size: 20pt, weight: "bold")[#d.nome] \
  #text(size: 11pt)[#d.titulo] \
  #d.contato.map(contato).join([  |  ])
]

#secao(d.rotulos.resumo)
#d.resumo

#secao(d.rotulos.experiencia)
#for e in d.experiencias [
  #strong[#e.empresa] #h(1fr) #e.periodo \
  #emph[#e.cargo]#if e.local != "" [, #e.local]
  #list(..e.bullets.map(b => [#b.texto]))
  #v(0.2em)
]

#secao(d.rotulos.skills)
#for g in d.skills [
  #strong[#g.categoria:] #g.itens.join(", ") \
]

#secao(d.rotulos.formacao)
#for f in d.formacao [
  #strong[#f.instituicao] #h(1fr) #f.periodo \
  #f.curso#if f.local != "" [, #f.local]
  #if f.detalhes != "" [ \ #f.detalhes]
  #v(0.2em)
]

#if d.certificacoes.len() > 0 [
  #secao(d.rotulos.certificacoes)
  #list(..d.certificacoes.map(c => [#c]))
]

#secao(d.rotulos.idiomas)
#d.idiomas.join("  |  ")
