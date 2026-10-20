// Σελίδα εισόδου: επιστροφή στη σελίδα που ζητήθηκε, μηνύματα λάθους, ειδοποίηση όταν είμαστε μέσα σε iframe.
'use strict';

document.addEventListener('DOMContentLoaded', () => {
  const p = new URLSearchParams(location.search);

  const next = p.get('next') || '/';
  // Μόνο διευθύνσεις του ίδιου site (ο server το ελέγχει ξανά).
  document.getElementById('next').value = next.startsWith('/') && !next.startsWith('//') ? next : '/';

  const messages = {
    '1': 'Λάθος κωδικός. Δοκιμάστε ξανά.',
    wait: 'Πάρα πολλές προσπάθειες. Δοκιμάστε ξανά σε λίγα λεπτά.',
  };
  const error = messages[p.get('error')];
  if (error) {
    const box = document.getElementById('error');
    box.textContent = error;
    box.classList.remove('d-none');
  }

  // Μέσα στη σελίδα του Hugging Face (iframe) ο browser μπορεί να μην κρατά το cookie εισόδου.
  if (window.top !== window.self) {
    document.getElementById('open-direct').href = location.origin + '/';
    document.getElementById('framed').classList.remove('d-none');
  }
});
