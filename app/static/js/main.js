document.addEventListener("DOMContentLoaded", () => {
    const cards = document.querySelectorAll(".game-card");

    cards.forEach((card, index) => {
        card.style.opacity = "0";
        card.style.transform = "translateY(12px)";

        setTimeout(() => {
            card.style.transition = "0.25s ease";
            card.style.opacity = "1";
            card.style.transform = "translateY(0)";
        }, index * 35);
    });
});