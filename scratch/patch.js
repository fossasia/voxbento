const fs = require('fs');
let content = fs.readFileSync('portal/translations/constants.py', 'utf8');
content = content.replace(
/<<<<<<< HEAD\n    TranslationProviderEnum\.GROQ\.value: \["openai\/gpt-oss-120b", "openai\/gpt-oss-20b", "groq\/compound-mini"\],\n=======\n    TranslationProviderEnum\.GROQ\.value: \["llama-3\.3-70b-versatile", "mixtral-8x7b-32768", "llama-3\.1-8b-instant"\],\n>>>>>>> upstream\/main/g,
'    TranslationProviderEnum.GROQ.value: ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound-mini"],'
);
fs.writeFileSync('portal/translations/constants.py', content);
