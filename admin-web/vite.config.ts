import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({base:'/admin/',plugins:[react()],server:{proxy:{'/api':{target:process.env.HOMEAI_ADMIN_API||'https://127.0.0.1:58443',secure:false}}}});
