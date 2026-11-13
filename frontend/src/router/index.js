export function route(){const [page='overview',first,second]=location.hash.replace(/^#\//,'').split('/');return {page,id:first,ids:{first,second}};}
