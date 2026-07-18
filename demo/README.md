# Sightline Static Demo

This folder contains the source for the static public Sightline demo. The generated site lives in `demo-site/` and can be hosted without FastAPI, SQLite, OCR, Tesseract, or any parser service. The build packages the shared Sightline theme and supplied web-ready brand assets with the demo.

Regenerate the site from the repository root:

```powershell
python demo/build_demo_site.py
```

Local smoke test:

```powershell
python -m http.server 4173 --directory demo-site
```

Open `http://127.0.0.1:4173/` and `http://127.0.0.1:4173/encounters`.

AWS hosting:

- Point AWS Amplify Hosting, S3 static website hosting, or CloudFront at `demo-site/`.
- No backend build command is required if you upload the generated folder directly.
- If deploying from the repository through Amplify, use `python demo/build_demo_site.py` as the build command and `demo-site` as the artifact directory.
- The generated `settings` and `replay` routes redirect to `/`; the visible UI hides those links in demo mode.
- Check the current [AWS Free Tier FAQ](https://aws.amazon.com/free/free-tier-faqs/) and [AWS Amplify pricing](https://aws.amazon.com/amplify/pricing/) before publishing, and set a budget alert even for static hosting.
