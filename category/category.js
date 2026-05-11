(function () {
  const PAGE_SIZE = 12;
  const grid = document.getElementById('catGrid');
  const pag = document.getElementById('catPagination');
  const search = document.getElementById('catSearch');
  const nav = document.querySelector('.category__nav');
  if (!grid || !pag) return;

  const VALID_CATS = ['all', 'fiz', 'yur', 'vzysk', 'news'];
  let all = [];
  let query = '';
  let cat = readCatFromUrl();
  let page = readPageFromUrl();

  function readCatFromUrl() {
    const u = new URL(window.location.href);
    const c = u.searchParams.get('cat');
    return VALID_CATS.includes(c) ? c : 'all';
  }

  function readPageFromUrl() {
    const u = new URL(window.location.href);
    return Math.max(1, parseInt(u.searchParams.get('page') || '1', 10));
  }

  function writeUrl({ push } = { push: true }) {
    const u = new URL(window.location.href);
    if (cat && cat !== 'all') u.searchParams.set('cat', cat);
    else u.searchParams.delete('cat');
    if (page > 1) u.searchParams.set('page', String(page));
    else u.searchParams.delete('page');
    const next = u.pathname + (u.search ? u.search : '') + u.hash;
    const cur = window.location.pathname + window.location.search + window.location.hash;
    if (next === cur) return;
    if (push) history.pushState({ cat, page }, '', next);
    else history.replaceState({ cat, page }, '', next);
  }

  function updateActiveChip() {
    if (!nav) return;
    nav.querySelectorAll('.category__nav-chip').forEach(el => {
      const c = el.dataset.cat || 'all';
      el.classList.toggle('is-active', c === cat);
    });
  }

  const render = () => {
    updateActiveChip();
    const q = query.trim().toLowerCase();
    const list = all
      .filter(a => cat === 'all' ? true : a.cat === cat)
      .filter(a => !q || (a.title + ' ' + (a.catLabel || '') + ' ' + (a.desc || '')).toLowerCase().includes(q));

    if (!list.length) {
      grid.innerHTML = '<div class="category__empty">' + (q ? 'Ничего не найдено по запросу.' : 'Пока нет статей в этой категории. Скоро появятся.') + '</div>';
      pag.innerHTML = '';
      return;
    }

    const pages = Math.ceil(list.length / PAGE_SIZE);
    const p = Math.min(q ? 1 : page, pages);
    const slice = list.slice((p - 1) * PAGE_SIZE, p * PAGE_SIZE);

    const isAbsoluteUrl = (s) => /^(https?:)?\/\//i.test(s || '');
    grid.innerHTML = slice.map(a => {
      const imgUrl = isAbsoluteUrl(a.img) ? a.img : ('../' + a.img);
      return `
      <a class="category-card" href="../${a.url}" style="background-image:linear-gradient(180deg, rgba(10,13,18,0.1) 0%, rgba(10,13,18,0.88) 100%), url('${imgUrl}')">
        <span class="category-card__tag">${a.catLabel}</span>
        <h2 class="category-card__title">${a.title}</h2>
        <div class="category-card__meta">${a.date} - ${a.read}</div>
      </a>`;
    }).join('');

    if (pages > 1 && !q) {
      const items = [];
      for (let i = 1; i <= pages; i++) {
        const u = new URL(window.location.href);
        if (cat && cat !== 'all') u.searchParams.set('cat', cat); else u.searchParams.delete('cat');
        if (i > 1) u.searchParams.set('page', String(i)); else u.searchParams.delete('page');
        items.push(`<a class="category__page${i === p ? ' is-active' : ''}" href="${u.pathname + u.search}" data-page="${i}">${i}</a>`);
      }
      pag.innerHTML = items.join('');
    } else {
      pag.innerHTML = '';
    }
  };

  fetch('../articles.json')
    .then(r => r.json())
    .then(data => {
      all = (data.articles || []).sort((a, b) => (b.dateIso || '').localeCompare(a.dateIso || ''));
      render();
    })
    .catch(() => {
      grid.innerHTML = '<div class="category__empty">Не удалось загрузить статьи.</div>';
    });

  if (search) {
    search.addEventListener('input', (e) => {
      query = e.target.value;
      render();
    });
  }

  if (nav) {
    nav.addEventListener('click', (e) => {
      const chip = e.target.closest('.category__nav-chip');
      if (!chip) return;
      e.preventDefault();
      const next = chip.dataset.cat || 'all';
      if (next === cat) return;
      cat = next;
      page = 1;
      writeUrl({ push: true });
      render();
    });
  }

  pag.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-page]');
    if (!link) return;
    e.preventDefault();
    page = parseInt(link.dataset.page, 10) || 1;
    writeUrl({ push: true });
    window.scrollTo({ top: 0, behavior: 'smooth' });
    render();
  });

  window.addEventListener('popstate', () => {
    cat = readCatFromUrl();
    page = readPageFromUrl();
    render();
  });
})();
