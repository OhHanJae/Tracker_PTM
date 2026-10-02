import {GLTFLoader} from './vendor/loaders/GLTFLoader.js';

const loader = new GLTFLoader();

export async function loadGlb(url)
{
    return new Promise((resolve, reject) => {
        loader.load(
            url,
            gltf => {
                if (!gltf?.scene)
                {
                    reject(new Error('GLB scene is missing.'));
                    return;
                }
                resolve(gltf);
            },
            undefined,
            error => reject(error instanceof Error ? error : new Error(String(error)))
        );
    });
}
