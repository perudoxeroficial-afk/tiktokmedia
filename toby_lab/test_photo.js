const Tiktok = require("@tobyg74/tiktok-api-dl");

async function main() {
    const url = process.argv[2];

    if (!url) {
        console.error('Uso: node test_photo.js "https://vt.tiktok.com/..."');
        process.exit(1);
    }

    try {
        const result = await Tiktok.Downloader(url, { version: "v1" });
        console.log(JSON.stringify(result, null, 2));
    } catch (error) {
        console.error(JSON.stringify({ status: "error", error: error?.message || String(error) }, null, 2));
        process.exit(1);
    }
}

main();
