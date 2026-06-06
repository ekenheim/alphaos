// Inspiration page — fetch the screenshots list, render gallery, lightbox on click.

(async function () {
  const Z = window.alphaos;
  try {
    const data = await Z.getJSON("/api/inspiration");
    renderGallery(data.images || []);
  } catch (e) {
    console.error(e);
  }

  function renderGallery(images) {
    const g = document.querySelector("#gallery");
    if (!images.length) {
      g.innerHTML = '<div class="muted" style="padding:60px 0;text-align:center">No screenshots found. Drop image files into the /screenshots folder.</div>';
      return;
    }
    g.innerHTML = images.map(src => `
      <a class="gallery-item" href="#" data-src="${src}">
        <img loading="lazy" src="${src}" alt="" />
      </a>
    `).join("");
    g.querySelectorAll(".gallery-item").forEach(a => {
      a.addEventListener("click", ev => {
        ev.preventDefault();
        openLightbox(a.dataset.src);
      });
    });
  }

  function openLightbox(src) {
    const lb = document.querySelector("#lightbox");
    document.querySelector("#lightbox-img").src = src;
    lb.classList.remove("hidden");
  }

  document.querySelector(".lightbox-close").addEventListener("click", () => {
    document.querySelector("#lightbox").classList.add("hidden");
  });
  document.querySelector("#lightbox").addEventListener("click", e => {
    if (e.target.id === "lightbox") {
      document.querySelector("#lightbox").classList.add("hidden");
    }
  });
})();
