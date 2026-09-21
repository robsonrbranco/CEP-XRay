CREATE TABLE TBL_CEP_2610_LOGRADOURO (
	CEP VARCHAR(8),
	TIPO VARCHAR(50),
	NOME_LOGRADOURO VARCHAR(100)
);
INSERT INTO TBL_CEP_2610_LOGRADOURO (CEP,TIPO,NOME_LOGRADOURO,LOGRADOURO,BAIRRO_ID,DISTRITO_ID,CIDADE_ID,ESTADO,TIPO_SA,NOME_LOGRADOURO_SA,LOGRADOURO_SA,LATITUDE,LONGITUDE,CEP_ATIVO) VALUES
	 ('01001000','Praça','da Sé','Praça da Sé',1,NULL,1,'SP','Praca','da Se','Praca da Se','-23.5503099','-46.6342009','S'),
	 ('01001001','Rua','Quebra
de linha','Rua Quebra
de linha',NULL,NULL,1,'SP','Rua','Quebra','Rua Quebra','-23.5','-46.6','S'),
	 ('01001002','Rua','D''Oeste','Rua D''Oeste',2,3,1,'SP','Rua','DOeste','Rua DOeste','-23.6','-46.7',NULL),
	 ('01001003','','','',NULL,NULL,1,'SP','','','','-23.7','-46.8',NULL),
	 ('01001004','Rua','  espaço nas pontas  ','Rua espaco',NULL,NULL,1,'SP','Rua','espaco','Rua espaco','-23.8','-46.9','S'),
	 ('01001005','Rua','Recanto das Águas','Rua Recanto',NULL,NULL,1,'SP','Rua','Recanto','Rua Recanto','-23.9','-47.0','S'),
	 ('01001006','Rua','Treze casas','Rua Treze',NULL,NULL,1,'SP','Rua','Treze','Rua Treze','-23.5628731234567','-46.6546812345678','S');
