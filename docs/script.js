// Animate elements on scroll
const observer = new IntersectionObserver((entries) => {
  entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('visible'); });
}, { threshold: 0.1 });

document.querySelectorAll('.card, .arch-plane, .usage-block, .bench-screenshot, .bench-table-wrap')
  .forEach(el => { el.classList.add('fade-in'); observer.observe(el); });

// Inject fade-in CSS
const s = document.createElement('style');
s.textContent = `.fade-in{opacity:0;transform:translateY(20px);transition:opacity .5s,transform .5s}.fade-in.visible{opacity:1;transform:translateY(0)}`;
document.head.appendChild(s);
